"""``text-extraction-init``: the one-shot ``init`` service of the root docker-compose.yml.

The root compose file must start without a single variable (Hostinger's Docker Manager copies
``.env.example`` to ``.env`` and offers no field for variables before the first deploy works). So
the stack's secrets are not variables: on its first start this command creates them in the
secrets volume, and every later start keeps them.

Files in ``--secrets-dir`` (default ``/secrets``):

- ``valkey-password`` (0600): 48 hex characters, created once.
- ``admin-key`` (0600): an admin API key of 64 hex characters, created once. Its plain value is
  logged only on the start that creates it; ``--show-admin-key`` prints it later.
- ``valkey.conf`` (0644, Valkey runs as its own user): the password of Valkey's default user and a
  user ``healthcheck`` without password that may only ``PING``, for the container's health check.
- ``valkey-url`` (0600): ``redis://:<password>@<valkey host>:6379/0``, read through
  ``VALKEY_URL_FILE``.
- ``api-keys.json`` (0600): the admin key's SHA-256 in the shape of ``API_KEYS``, read through
  ``API_KEYS_FILE`` (merged with ``API_KEYS`` from ``.env``).

The last three are written again from the first two on every start (same content). A ``DOMAIN``
in the environment must be a bare host name: the Traefik labels take it unchanged, and
``https://...`` there breaks the route without an error at deploy time, so the command exits 1.

The command does not read ``Settings``: they need API keys, which do not exist yet. It never logs
the Valkey password.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from text_extraction.logging import configure_logging

_log = structlog.get_logger(__name__)

DEFAULT_SECRETS_DIR = Path("/secrets")
_SECRET = re.compile(r"^[0-9A-Za-z]{32,}$")
# A DNS host name: labels of letters, digits and inner hyphens, dot-separated, no scheme, port,
# path or trailing dot.
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class SecretsError(Exception):
    """A secret file exists but cannot be used. The message never contains a secret."""


@dataclass(frozen=True)
class SecretsResult:
    admin_key_created: str | None  # the plain key, only on the run that created it


def domain_problem(domain: str) -> str | None:
    """Why ``domain`` cannot go into a Traefik ``Host()`` rule, or None (empty is fine)."""
    if not domain:
        return None
    labels = domain.split(".")
    if len(domain) > 253 or not all(_HOST_LABEL.match(label) for label in labels):
        return (
            "DOMAIN must be a bare host name such as api.example.org "
            "(no https://, port, path or slash)"
        )
    return None


def _write(path: Path, content: str, mode: int) -> None:
    """Write ``content`` atomically with ``mode``; leave the file alone when it is unchanged."""
    try:
        if path.read_text(encoding="utf-8") == content and (path.stat().st_mode & 0o777) == mode:
            return
    except FileNotFoundError:
        pass
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(content)
    os.chmod(temporary, mode)  # the umask may have narrowed it
    os.replace(temporary, path)


def _kept_secret(path: Path, length_bytes: int) -> tuple[str, bool]:
    """The secret in ``path``, or a new one written there. Returns (secret, created)."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if value:
        if not _SECRET.match(value):
            raise SecretsError(
                f"{path.name} in {path.parent} is not 32 or more letters and digits; "
                "fix or delete the file"
            )
        return value, False
    value = secrets.token_hex(length_bytes)
    _write(path, value + "\n", 0o600)
    return value, True


def ensure_secrets(directory: Path, valkey_host: str = "valkey") -> SecretsResult:
    """Create the missing secrets in ``directory`` and write the files derived from them."""
    password, _ = _kept_secret(directory / "valkey-password", 24)
    admin_key, admin_created = _kept_secret(directory / "admin-key", 32)

    _write(
        directory / "valkey.conf",
        "# Written by text-extraction-init from valkey-password on every start.\n"
        f"user default on >{password} ~* &* +@all\n"
        "user healthcheck on nopass -@all +ping\n",
        0o644,
    )
    _write(directory / "valkey-url", f"redis://:{password}@{valkey_host}:6379/0\n", 0o600)
    keys = [
        {
            "name": "admin",
            "role": "admin",
            "sha256": hashlib.sha256(admin_key.encode()).hexdigest(),
        }
    ]
    _write(directory / "api-keys.json", json.dumps(keys) + "\n", 0o600)
    return SecretsResult(admin_key_created=admin_key if admin_created else None)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO")
    fmt: Literal["json", "console"] = (
        "console" if os.environ.get("LOG_FORMAT", "json").lower() == "console" else "json"
    )
    try:
        configure_logging(level, fmt)
    except ValueError:
        configure_logging("INFO", fmt)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-init",
        description="Create the secrets of the root docker-compose.yml on its first start.",
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=DEFAULT_SECRETS_DIR,
        help="Directory of the secrets volume (default /secrets).",
    )
    parser.add_argument(
        "--valkey-host",
        default="valkey",
        help="Host name of Valkey in the generated URL (default valkey).",
    )
    parser.add_argument(
        "--show-admin-key",
        action="store_true",
        help="Print the generated admin API key and exit (1: there is none yet).",
    )
    args = parser.parse_args(argv)

    if args.show_admin_key:
        try:
            print((args.secrets_dir / "admin-key").read_text(encoding="utf-8").strip())
        except OSError:
            print(f"no admin key in {args.secrets_dir}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    _configure_logging()
    problem = domain_problem(os.environ.get("DOMAIN", "").strip())
    if problem:
        _log.error("init_domain_invalid", reason=problem)
        sys.exit(1)
    try:
        result = ensure_secrets(args.secrets_dir, args.valkey_host)
    except (SecretsError, OSError) as error:
        reason = str(error) if isinstance(error, SecretsError) else error.strerror
        _log.error("init_secrets_failed", secrets_dir=str(args.secrets_dir), reason=reason)
        sys.exit(1)
    if result.admin_key_created:
        # Shown once, on purpose: the log is where a Docker panel user finds it.
        _log.warning(
            "init_admin_key_created",
            admin_key=result.admin_key_created,
            note="Send it as 'Authorization: Bearer <key>' or enter it in the playground. "
            "It is shown only now; later: docker compose run --rm -T init "
            "text-extraction-init --show-admin-key",
        )
    else:
        _log.info(
            "init_secrets_present",
            secrets_dir=str(args.secrets_dir),
            note="Admin key unchanged; show it with: docker compose run --rm -T init "
            "text-extraction-init --show-admin-key",
        )
    sys.exit(0)
