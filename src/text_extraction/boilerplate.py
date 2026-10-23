"""Per-domain boilerplate lines (navigation, footers, language switchers) removed from the text.

The lists are data, not code: lines that occur near-verbatim on almost every page of a domain,
learned from real extraction runs and shipped as package data
(``data/domain_boilerplate.json``, see NOTICE). A line is removed only when it equals a rule
exactly after trimming its whitespace, so body text that merely contains a rule is untouched.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from types import MappingProxyType

_DATA = "data/domain_boilerplate.json"


def _canonical_host(host: str) -> str:
    """Lower case, no trailing dot and no leading ``www.``: ``www.x.org`` and ``x.org`` are one."""
    return host.strip().lower().rstrip(".").removeprefix("www.")


@cache
def load_boilerplate_rules() -> Mapping[str, frozenset[str]]:
    """The packaged rules by host (without ``www.``). Read once, shared and read-only."""
    raw: dict[str, list[str]] = json.loads(
        files("text_extraction").joinpath(_DATA).read_text(encoding="utf-8")
    )
    merged: dict[str, set[str]] = {}
    for host, lines in raw.items():
        merged.setdefault(_canonical_host(host), set()).update(lines)
    return MappingProxyType({host: frozenset(lines) for host, lines in merged.items()})


def strip_boilerplate(host: str, text: str, rules: Mapping[str, frozenset[str]]) -> str:
    """Drop the lines of ``text`` that are one of the rules of ``host``.

    The rule host matches the exact host or the host without ``www.``. A text that loses no
    line is returned as it is; otherwise the remaining lines keep their line endings.
    """
    wanted = _canonical_host(host)
    blocked = rules.get(wanted) or rules.get(f"www.{wanted}")
    if not blocked or not text:
        return text
    return "".join(line for line in text.splitlines(keepends=True) if line.strip() not in blocked)
