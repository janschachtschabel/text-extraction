"""Licence gate (CLAUDE.md rule 7): no GPL or AGPL distribution in this environment.

Run it with the environment's interpreter, as CI does::

    uv run python scripts/check_licenses.py --lock uv.lock --extra browser

It reads the metadata of every installed distribution (``importlib.metadata``; ``--path`` reads
other directories instead) and judges its licence:

- ``License-Expression`` (SPDX, PEP 639) decides when present. ``OR`` takes the best
  alternative, ``AND`` and ``WITH`` the worst part.
- Otherwise the ``License ::`` classifiers and the first line of the free-text ``License``
  field (a name, or the title of a licence text), the worst of them.

GPL and AGPL are errors (exit code 1). LGPL is a warning: it is allowed as a library that is
only imported, which is how Python loads it. A distribution without any licence metadata is a
warning as well.

``--allowlist`` (default ``scripts/license-allowlist.toml``; an empty value means none) holds
reviewed exceptions: ``[[allow]]`` excuses a false positive of one distribution at one version
(``name``, ``version``, ``license``, ``reason``); ``[[not_installed]]`` lists packages of
``uv.lock`` that this environment does not install (another platform's packages) with their
reviewed licence (``name``, ``license``, ``reason``, optional ``version``).

``--lock`` checks that coverage: every package the lock reaches from the project's
dependencies, its dependency groups and the extras named with ``--extra`` (markers ignored)
must be installed here, so its metadata was judged, or be listed under ``[[not_installed]]``.
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from importlib import metadata
from pathlib import Path
from typing import Any, Final, Literal

DEFAULT_ALLOWLIST: Final = Path(__file__).with_name("license-allowlist.toml")

type Level = Literal["ok", "weak", "strong", "unknown"]

_RANK: Final[dict[Level, int]] = {"ok": 0, "unknown": 1, "weak": 2, "strong": 3}
_TOKENS: Final = re.compile(r"\(|\)|[^\s()]+")
_LESSER_TEXT: Final = re.compile(
    r"\bLGPL|LESSER GENERAL PUBLIC|LIBRARY GENERAL PUBLIC|LIBRARY OR LESSER", re.IGNORECASE
)
_STRONG_TEXT: Final = re.compile(r"\bA?GPL|GENERAL PUBLIC LICEN[CS]E", re.IGNORECASE)
_ALLOW_KEYS: Final = ("name", "version", "license", "reason")
_NOT_INSTALLED_KEYS: Final = ("name", "license", "reason")


@dataclass(frozen=True)
class Finding:
    """The verdict on one distribution and what it is based on."""

    name: str
    version: str
    level: Level
    evidence: str


def normalise(name: str) -> str:
    """The PEP 503 form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _worst(levels: Iterable[Level]) -> Level:
    return max(levels, key=_RANK.__getitem__, default="ok")


def _best(levels: Iterable[Level]) -> Level:
    return min(levels, key=_RANK.__getitem__, default="ok")


def _spdx_id(identifier: str) -> Level:
    upper = identifier.upper()
    if upper.startswith("LGPL"):
        return "weak"
    if upper.startswith(("GPL", "AGPL")):
        return "strong"
    return "ok"


def spdx_level(expression: str) -> Level:
    """The level of an SPDX licence expression: OR takes the best, AND the worst."""
    tokens = _TOKENS.findall(expression)
    position = 0

    def parse_or() -> Level:
        nonlocal position
        alternatives = [parse_and()]
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            alternatives.append(parse_and())
        return _best(alternatives)

    def parse_and() -> Level:
        nonlocal position
        parts = [parse_with()]
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            parts.append(parse_with())
        return _worst(parts)

    def parse_with() -> Level:
        nonlocal position
        if position >= len(tokens):
            return "unknown"
        word = tokens[position]
        position += 1
        if word == "(":
            level = parse_or()
            if position < len(tokens) and tokens[position] == ")":
                position += 1
        else:
            level = _spdx_id(word)
        if position < len(tokens) and tokens[position].upper() == "WITH":
            position += 2  # an exception narrows a licence, it never makes it permissive
        return level

    return parse_or()


def text_level(text: str) -> Level:
    """The level of a classifier or a free-text ``License`` field."""
    if _LESSER_TEXT.search(text):
        return "weak"
    if _STRONG_TEXT.search(text):
        return "strong"
    return "ok"


def judge(meta: Message) -> tuple[Level, str]:
    """The level of a distribution's metadata and the evidence (the text that decided)."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return spdx_level(expression), expression
    classifiers = [
        value for value in meta.get_all("Classifier") or [] if value.startswith("License ::")
    ]
    field = (meta.get("License") or "").strip()
    if field.upper() in {"", "UNKNOWN"}:
        field = ""
    candidates = [(text_level(value), value) for value in classifiers]
    if field:
        # A licence text says what it is in its first line ("BSD 3-Clause License", "GNU
        # GENERAL PUBLIC LICENSE"); later lines may name other licences (pandas bundles
        # Python's licence, which explains GPL compatibility).
        title = next(line.strip() for line in field.splitlines() if line.strip())[:120]
        candidates.append((text_level(title), title))
    if not candidates:
        return "unknown", "no licence metadata"
    return max(candidates, key=lambda candidate: _RANK[candidate[0]])


@dataclass(frozen=True)
class Allowlist:
    """Reviewed exceptions, by normalised name."""

    allow: Mapping[str, Mapping[str, str]]
    not_installed: Mapping[str, Mapping[str, str]]


def load_allowlist(path: Path | None) -> Allowlist:
    if path is None:
        return Allowlist({}, {})
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Allowlist(
        _entries(data.get("allow", []), _ALLOW_KEYS, "allow"),
        _entries(data.get("not_installed", []), _NOT_INSTALLED_KEYS, "not_installed"),
    )


def _entries(raw: Any, keys: Sequence[str], table: str) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for entry in raw:
        missing = [key for key in keys if not isinstance(entry.get(key), str) or not entry[key]]
        if missing:
            sys.exit(f"license allowlist: a [[{table}]] entry lacks {', '.join(missing)}")
        entries[normalise(entry["name"])] = {key: str(value) for key, value in entry.items()}
    return entries


def installed(paths: Sequence[str] | None) -> dict[str, metadata.Distribution]:
    """The installed distributions by normalised name (the first one on the path wins)."""
    found: dict[str, metadata.Distribution] = {}
    dists = metadata.distributions(path=list(paths)) if paths else metadata.distributions()
    for dist in dists:
        name = dist.metadata["Name"]
        if name:
            found.setdefault(normalise(name), dist)
    return found


def lock_packages(lock: Mapping[str, Any], extras: Sequence[str]) -> dict[str, str]:
    """The packages (name: version) the lock reaches from the project: its dependencies, its
    dependency groups and the given extras, following the extras of each dependency."""
    packages = {normalise(package["name"]): package for package in lock.get("package", [])}
    roots = [
        package
        for package in packages.values()
        if {"editable", "virtual"} & set(package.get("source", {}))
    ]
    pending: list[tuple[str, tuple[str, ...]]] = []
    for root in roots:
        groups = [root.get("dependencies", [])]
        groups.extend(root.get("dev-dependencies", {}).values())
        groups.extend(root.get("optional-dependencies", {}).get(extra, []) for extra in extras)
        pending.extend(_edges(dependency) for group in groups for dependency in group)
    reached: dict[str, set[str]] = {}
    while pending:
        name, wanted = pending.pop()
        seen = reached.get(name)
        if seen is not None and set(wanted) <= seen:
            continue
        reached.setdefault(name, set()).update(wanted)
        package = packages.get(name, {})
        groups = [package.get("dependencies", [])]
        groups.extend(package.get("optional-dependencies", {}).get(extra, []) for extra in wanted)
        pending.extend(_edges(dependency) for group in groups for dependency in group)
    root_names = {normalise(root["name"]) for root in roots}
    return {
        name: str(packages[name].get("version", "?"))
        for name in reached
        if name in packages and name not in root_names
    }


def _edges(dependency: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    return normalise(dependency["name"]), tuple(dependency.get("extra", []))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", action="append", help="read distributions from this directory")
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help="reviewed exceptions (TOML); an empty value means none",
    )
    parser.add_argument("--lock", help="uv.lock whose packages must be covered")
    parser.add_argument("--extra", action="append", default=[], help="an extra of the lock")
    args = parser.parse_args(argv)
    allowlist = load_allowlist(Path(args.allowlist) if args.allowlist else None)

    errors = warnings = 0
    dists = installed(args.path)
    for name, dist in sorted(dists.items()):
        version = dist.version
        # PackageMetadata is an email Message at run time; its protocol lacks the class.
        level, evidence = judge(dist.metadata)  # type: ignore[arg-type]
        display = dist.metadata["Name"]
        allowed = allowlist.allow.get(name)
        if level in ("strong", "weak") and allowed and allowed.get("version") == version:
            print(f"allowed {display} {version}: {allowed['license']} ({allowed['reason']})")
        elif level == "strong":
            errors += 1
            print(f"ERROR {display} {version}: GPL or AGPL licence ({evidence})")
        elif level == "weak":
            warnings += 1
            print(f"WARNING {display} {version}: LGPL licence ({evidence})")
        elif level == "unknown":
            warnings += 1
            print(f"WARNING {display} {version}: {evidence}")

    if args.lock:
        lock = tomllib.loads(Path(args.lock).read_text(encoding="utf-8"))
        for name, version in sorted(lock_packages(lock, args.extra).items()):
            if name in dists:
                continue
            reviewed = allowlist.not_installed.get(name)
            if reviewed and reviewed.get("version", version) == version:
                print(
                    f"not installed {name} {version}: {reviewed['license']} ({reviewed['reason']})"
                )
            else:
                errors += 1
                print(
                    f"ERROR {name} {version}: in uv.lock but not installed here; review its "
                    "licence and add it to [[not_installed]] in the allowlist"
                )

    print(f"checked {len(dists)} distributions: {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
