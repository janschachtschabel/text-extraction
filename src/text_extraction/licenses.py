"""Licence ids and the ranking of the licences a page declares.

Licences get SPDX-style ids (``CC-BY-SA-4.0``). ``LicenseCollector`` gathers the candidates a
page offers, each with the source it came from and how far to trust it: the page's own
``rel=license`` link or structured ``license`` property is ``high``, a meta tag or OpenGraph
property ``medium``, and a licence mentioned in the footer ``low``. A document's XMP packet
(``xmp``) counts like structured data: ``cc:license`` is ``high``. The best candidate has the
highest confidence, then a recognised id, then comes first.
"""

import re
from collections.abc import Iterable, Iterator
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from text_extraction.convert.links import absolute_url

LicenseSource = Literal[
    "rel-license", "json-ld", "microdata", "rdfa", "meta", "opengraph", "text", "xmp"
]
Confidence = Literal["high", "medium", "low"]

_MAX_LICENSES = 10  # kept in the result
_MAX_CANDIDATES = 200  # looked at: a page full of licence links costs nothing more
_MAX_VALUES = 20  # of one structured property
_MAX_VALUE_LENGTH = 2000
_MAX_LABEL_LENGTH = 200
_MAX_FOOTER_ANCHORS = 100

_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 0, "medium": 1, "low": 2}

# --- ids ---------------------------------------------------------------------------------------

# 2.1 and 2.5 exist for some jurisdiction ports only; the port is dropped, the version stays.
_VERSIONS = frozenset({"1.0", "2.0", "2.1", "2.5", "3.0", "4.0"})
_SEPARATOR = r"[\s\-_\u2010-\u2015]"
_URL_LICENSE = re.compile(
    r"(?<![a-z0-9-])creativecommons\.org/(?:"
    r"licen[sc]es/(?P<kind>by(?:-(?:nc|sa|nd))*)/(?P<version>\d\.\d)(?!\d)"
    r"|publicdomain/(?P<public>zero|mark)/1\.0(?!\d))",
    re.IGNORECASE,
)
_SPDX_URL = re.compile(
    r"(?<![a-z0-9-])spdx\.org/licenses/(?P<id>[\w.+-]+?)(?:\.html|\.json)?(?:$|[?#])"
)
_SHORT_LICENSE = re.compile(
    rf"(?<![a-z0-9])(?:cc|creative{_SEPARATOR}+commons){_SEPARATOR}*by"
    rf"(?P<mods>(?:{_SEPARATOR}*(?:nc|sa|nd))*)"
    rf"{_SEPARATOR}*v?(?P<version>\d\.\d)(?!\d)",
    re.IGNORECASE,
)
_ZERO_LICENSE = re.compile(
    rf"(?<![a-z0-9])(?:cc{_SEPARATOR}?(?:0|zero)|creative{_SEPARATOR}+commons{_SEPARATOR}+zero)"
    r"(?![0-9a-z]|[,.]\d)",
    re.IGNORECASE,
)
_MARK_LICENSE = re.compile(
    rf"public\s+domain\s+mark|(?<![a-z0-9])pdm{_SEPARATOR}*1\.0(?!\d)", re.IGNORECASE
)
# Words of the long names, in English and German; CamelCase forms (NichtKommerziell) too.
_NON_COMMERCIAL = r"non[\s\-]?commercial|nicht[\s\-]*kommerziell\w*"
_SHARE_ALIKE = r"share[\s\-]?alike|weitergabe[\s\-]*unter[\s\-]*gleichen[\s\-]*bedingungen"
_NO_DERIVATIVES = r"no[\s\-]*deriv(?:ative)?s?(?:[\s\-]+works?)?|keine[\s\-]*bearbeitung(?:en)?"
_LONG_SEPARATOR = r"[\s\-,\u2010-\u2015]"
_LONG_LICENSE = re.compile(
    rf"\b(?:attribution|namensnennung)"
    rf"(?P<mods>(?:{_LONG_SEPARATOR}*(?:{_NON_COMMERCIAL}|{_SHARE_ALIKE}|{_NO_DERIVATIVES}))*)"
    rf"{_LONG_SEPARATOR}*(?:licen[sc]e\s+|lizenz\s+)?v?(?P<version>\d\.\d)(?!\d)",
    re.IGNORECASE,
)
_MODIFIER_WORDS = (
    ("nc", re.compile(_NON_COMMERCIAL, re.IGNORECASE)),
    ("sa", re.compile(_SHARE_ALIKE, re.IGNORECASE)),
    ("nd", re.compile(_NO_DERIVATIVES, re.IGNORECASE)),
)


def _cc_id(mods: set[str], version: str) -> str | None:
    """``CC-BY-NC-SA-4.0`` from the lower-case modifiers; None for an impossible combination."""
    if version not in _VERSIONS or {"sa", "nd"} <= mods:
        return None
    parts = ["CC", "BY"] + [part.upper() for part in ("nc", "sa", "nd") if part in mods]
    return "-".join([*parts, version])


def _short_mods(text: str) -> set[str]:
    return set(re.findall(r"nc|sa|nd", text.lower()))


def _long_mods(text: str) -> set[str]:
    return {key for key, pattern in _MODIFIER_WORDS if pattern.search(text)}


def _scan_licenses(text: str) -> list[tuple[str, str]]:
    """The recognised licences in the text as (id, matched text), in order of appearance."""
    found: list[tuple[int, str, str]] = []
    for match in _SHORT_LICENSE.finditer(text):
        license_id = _cc_id(_short_mods(match["mods"]), match["version"])
        if license_id:
            found.append((match.start(), license_id, match.group().strip()))
    for match in _LONG_LICENSE.finditer(text):
        license_id = _cc_id(_long_mods(match["mods"]), match["version"])
        if license_id:
            found.append((match.start(), license_id, match.group().strip()))
    for match in _ZERO_LICENSE.finditer(text):
        found.append((match.start(), "CC0-1.0", match.group().strip()))
    for match in _MARK_LICENSE.finditer(text):
        found.append((match.start(), "PDM-1.0", match.group().strip()))
    return [(license_id, snippet) for _, license_id, snippet in sorted(found)]


def license_from_url(url: str) -> str | None:
    """The id behind a creativecommons.org or spdx.org licence URL; None for any other string.

    Unlike ``normalize_license`` this never reads prose into a URL: ``/tag/cc0`` is no licence."""
    match = _URL_LICENSE.search(url)
    if match:
        if match["public"]:
            return "CC0-1.0" if match["public"].lower() == "zero" else "PDM-1.0"
        return _cc_id(_short_mods(match["kind"][2:]), match["version"])
    spdx = _SPDX_URL.search(url)
    if spdx:
        found = _scan_licenses(spdx["id"])
        return found[0][0] if found else None
    return None


def normalize_license(value: str) -> str | None:
    """The SPDX-style id of a Creative Commons licence or public-domain tool, or None.

    Accepts creativecommons.org URLs of any version, jurisdiction port and scheme, and the usual
    texts: ``CC BY-SA 4.0``, ``Creative Commons Attribution-ShareAlike 4.0 International``,
    ``Namensnennung - Weitergabe unter gleichen Bedingungen 4.0``, ``CC0``, ``Public Domain
    Mark``. The jurisdiction is dropped (``CC-BY-3.0``, not ``CC-BY-3.0-DE``). A licence without
    a version has no valid id and gives None."""
    license_id = license_from_url(value)
    if license_id:
        return license_id
    found = _scan_licenses(value)
    return found[0][0] if found else None


# --- candidates --------------------------------------------------------------------------------


class License(BaseModel):
    id: str | None  # SPDX-style, e.g. CC-BY-SA-4.0, CC0-1.0, PDM-1.0
    url: str | None
    label: str | None
    source: LicenseSource
    confidence: Confidence


def link_header_licenses(header: str) -> Iterator[str]:
    """Targets of ``Link: <url>; rel="license"`` in a ``Link`` response header."""
    for match in re.finditer(r"<([^>]*)>([^<]*)", header):
        rel = re.search(r'rel\s*=\s*(?:"([^"]*)"|([^\s;,]+))', match[2], re.IGNORECASE)
        relations = (rel[1] or rel[2] or "").lower().split() if rel else []
        if "license" in relations:
            yield match[1].strip()


class LicenseCollector:
    """Candidates in the order they are found; ``result`` ranks them."""

    def __init__(self, base: str) -> None:
        self._base = base
        self._found: list[License] = []

    def add(
        self,
        value: str,
        source: LicenseSource,
        confidence: Confidence,
        *,
        label: str | None = None,
        is_url: bool | None = None,
    ) -> None:
        """One candidate: a URL (resolved against the page, read by ``license_from_url`` only)
        or a text (read by ``normalize_license``). ``is_url`` None guesses from the value."""
        value = " ".join(value.split())
        if not value or len(value) > _MAX_VALUE_LENGTH or len(self._found) >= _MAX_CANDIDATES:
            return
        if is_url is None:
            is_url = bool(re.match(r"(?:https?:)?//\S+$", value))
        label = label[:_MAX_LABEL_LENGTH] if label else None
        if is_url:
            url = absolute_url(self._base, value)
            if not url or urlsplit(url).scheme not in {"http", "https"}:
                return
            candidate = License(
                id=license_from_url(url), url=url, label=label, source=source, confidence=confidence
            )
        else:
            candidate = License(
                id=normalize_license(value),
                url=None,
                label=label or value[:_MAX_LABEL_LENGTH],
                source=source,
                confidence=confidence,
            )
        self._found.append(candidate)

    def add_property(
        self, value: Any, source: LicenseSource, confidence: Confidence, depth: int = 0
    ) -> None:
        """A structured ``license`` property: a URL, a text, an object with ``@id``/``url`` and
        ``name``, a ``@value`` literal (RDFa), or a list of these."""
        if depth > 3:
            return
        if isinstance(value, str):
            self.add(value, source, confidence)
        elif isinstance(value, list):
            for item in value[:_MAX_VALUES]:
                self.add_property(item, source, confidence, depth + 1)
        elif isinstance(value, dict):
            if "@value" in value:
                self.add_property(value["@value"], source, confidence, depth + 1)
                return
            reference = value.get("@id") or value.get("url")
            name = value.get("name")
            label = name if isinstance(name, str) else None
            if isinstance(reference, str):
                self.add(reference, source, confidence, label=label)
            elif label:
                self.add(label, source, confidence)

    def add_footer(self, text: str, anchors: Iterable[tuple[str, str]]) -> None:
        """Licences in a footer, ``low`` confidence: licence URLs of its links (anchors first,
        so that the URL is kept) and licence names in its text."""
        for href, label in list(anchors)[:_MAX_FOOTER_ANCHORS]:
            if license_from_url(href):
                self.add(href, "text", "low", label=label or None, is_url=True)
        for _, snippet in _scan_licenses(text):
            self.add(snippet, "text", "low")

    def result(self) -> list[License]:
        """Each licence once, with its best confidence; best first, at most ten."""
        best: dict[str, License] = {}
        for candidate in self._found:
            key = candidate.id or candidate.url or candidate.label or ""
            known = best.get(key)
            if known is None or _rank(candidate) < _rank(known):
                best[key] = candidate
        return sorted(best.values(), key=lambda item: (_rank(item), item.id is None))[
            :_MAX_LICENSES
        ]


def _rank(candidate: License) -> int:
    return _CONFIDENCE_RANK[candidate.confidence]
