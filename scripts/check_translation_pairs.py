"""Fail a pull request whose English user text changed without its German partner (ruling
P12-S12.2).

The pairs are ``README.md`` with ``README.de.md``, each page of ``docs/user-guide/en/`` with the
page of the same name in ``docs/user-guide/de/``, and the UI catalogue ``en.json`` with
``de.json``. A German-only change passes. A commit of the range with the trailer
``I18n-Unchanged: <path>`` exempts that English path, for a typo fix that needs no translation.

Run from the repository's root: ``python scripts/check_translation_pairs.py --base <ref>``; the
range is from the merge base of ``<ref>`` and ``HEAD`` to ``HEAD``.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence

TRAILER = "I18n-Unchanged"
README_PAIR = ("README.md", "README.de.md")
GUIDE_EN = "docs/user-guide/en/"
GUIDE_DE = "docs/user-guide/de/"
CATALOGUE_PAIR = (
    "src/text_extraction/ui/locales/en.json",
    "src/text_extraction/ui/locales/de.json",
)


def german_partner(path: str) -> str | None:
    """The German file of an English one, or None for a file that is no English half of a
    pair."""
    for english, german in (README_PAIR, CATALOGUE_PAIR):
        if path == english:
            return german
    if path.startswith(GUIDE_EN) and path.endswith(".md"):
        return GUIDE_DE + path.removeprefix(GUIDE_EN)
    return None


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changed_files(base: str) -> set[str]:
    """The files changed (added, modified or deleted) from the merge base to ``HEAD``."""
    out = _git("diff", "--name-only", "--no-renames", f"{base}...HEAD")
    return {line for line in out.splitlines() if line}


def exempted_paths(base: str) -> set[str]:
    """The paths that the trailer names in the commits of the range."""
    out = _git("log", f"--format=%(trailers:key={TRAILER},valueonly)", f"{base}..HEAD")
    return {line.strip() for line in out.splitlines() if line.strip()}


def unpaired_changes(changed: set[str], exempted: set[str]) -> list[tuple[str, str]]:
    """Each changed English file whose German partner did not change: (English, German)."""
    problems = []
    for path in sorted(changed):
        partner = german_partner(path)
        if partner is not None and partner not in changed and path not in exempted:
            problems.append((path, partner))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base", required=True, help="the ref the pull request merges into")
    args = parser.parse_args(argv)

    problems = unpaired_changes(changed_files(args.base), exempted_paths(args.base))
    for english, german in problems:
        print(
            f"{english} changed, but {german} did not: change both, or add the trailer "
            f"'{TRAILER}: {english}' to a commit when the German text needs no change."
        )
    if problems:
        return 1
    print("translation pairs: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
