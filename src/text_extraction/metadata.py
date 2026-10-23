"""What a page says about itself: title, authors, dates, licence, LRMI fields, images, language.

Everything here is read from the markup the page already carries. JSON-LD, microdata and RDFa
come from extruct, OpenGraph and meta tags straight from the DOM. Each source can be broken or
hostile, so a failing reader costs the page that one source and nothing else.

Licences get SPDX-style ids (``CC-BY-SA-4.0``) with the source they came from and how far to
trust them: the page's own ``rel=license`` link or structured ``license`` property is ``high``,
a meta tag or OpenGraph property ``medium``, and a licence mentioned in the footer ``low``.

``extract_metadata`` is synchronous and CPU-bound: callers run it in a thread.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from bs4 import BeautifulSoup, Tag
from extruct.jsonld import JsonLdExtractor
from extruct.rdfa import RDFaExtractor
from extruct.utils import parse_html as parse_lxml
from extruct.utils import parse_xmldom_html
from extruct.w3cmicrodata import MicrodataExtractor
from py3langid.langid import MODEL_FILE, LanguageIdentifier
from pydantic import BaseModel, Field

from text_extraction.convert.links import absolute_url, base_url_of
from text_extraction.convert.markup import attr, parse_html

_log = structlog.get_logger(__name__)

LicenseSource = Literal["rel-license", "json-ld", "microdata", "rdfa", "meta", "opengraph", "text"]
Confidence = Literal["high", "medium", "low"]

MAX_FIELD_LENGTH = 1000  # page-controlled text: keeps metadata from carrying a second document
MAX_IMAGES = 20
_MAX_LIST_ITEMS = 50
_MAX_LICENSES = 10
_MAX_WALK_DEPTH = 12
_MAX_FOOTER_CHARS = 5000
_MAX_FOOTERS = 10
# pyrdfa needs about 1.2 s per MB of markup; larger pages go without RDFa.
_RDFA_MAX_CHARS = 1_000_000
# RDFa attributes other than the ``property="og:..."`` every OpenGraph page has.
_RDFA_HINT = re.compile(
    r"""\b(?:typeof|vocab|prefix|about|resource|datatype)\s*=
    |\b(?:rel|rev|property)\s*=\s*["']?(?!og:|article:|twitter:|fb:|music:|video:|book:|profile:)
        [\w.-]+:[\w.-]+""",
    re.IGNORECASE | re.VERBOSE,
)
_MIN_DETECTION_WORDS = 50
_MAX_DETECTION_CHARS = 10_000
_MIN_DETECTION_CONFIDENCE = 0.5

_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 0, "medium": 1, "low": 2}

# Markup of a media item or a person carries its own licence, not the page's.
_OTHER_ITEM_TYPES = frozenset(
    {"ImageObject", "VideoObject", "AudioObject", "MediaObject", "Photograph", "Comment", "Review"}
)
# JSON-LD nodes that describe the site or the publisher rather than this page.
_SITE_NODE_TYPES = frozenset(
    {
        "WebSite",
        "Organization",
        "EducationalOrganization",
        "Corporation",
        "LocalBusiness",
        "Person",
        "BreadcrumbList",
        "ListItem",
        "SiteNavigationElement",
        "ImageObject",
    }
)
_LRMI_KEYS = frozenset(
    {
        "educationalLevel",
        "learningResourceType",
        "audience",
        "educationalRole",
        "typicalAgeRange",
        "teaches",
        "educationalAlignment",
        "inLanguage",
        "isAccessibleForFree",
        "timeRequired",
        "interactivityType",
    }
)
_LICENSE_META_NAMES = frozenset(
    {"license", "licence", "dc.license", "dcterms.license", "dc.rights", "dcterms.rights", "rights"}
)
_PUBLISHED_META = (
    "article:published_time",
    "og:article:published_time",
    "datepublished",
    "dcterms.created",
    "dc.date.issued",
    "dcterms.issued",
    "citation_publication_date",
    "dc.date",
    "dcterms.date",
    "date",
    "pubdate",
    "publishdate",
)
_MODIFIED_META = (
    "article:modified_time",
    "og:updated_time",
    "datemodified",
    "dcterms.modified",
    "dc.date.modified",
    "last-modified",
)
_AUTHOR_META = ("author", "article:author", "dc.creator", "dcterms.creator", "citation_author")
_OPENGRAPH_LICENSE_NAMES = frozenset({"og:license", "article:license"})
_LAZY_IMAGE_ATTRIBUTES = ("src", "data-src", "data-original", "data-lazy-src")

# --- licence ids -------------------------------------------------------------------------------

_VERSIONS = frozenset({"1.0", "2.0", "2.5", "3.0", "4.0"})
_SEPARATOR = r"[\s\-_\u2010-\u2015]"
_URL_LICENSE = re.compile(
    r"(?<![a-z0-9-])creativecommons\.org/(?:"
    r"licen[sc]es/(?P<kind>by(?:-nc)?(?:-sa|-nd)?)/(?P<version>\d\.\d)(?!\d)"
    r"|publicdomain/(?P<public>zero|mark)/1\.0(?!\d))",
    re.IGNORECASE,
)
_SHORT_LICENSE = re.compile(
    rf"(?<![a-z0-9])cc{_SEPARATOR}*by(?P<mods>(?:{_SEPARATOR}*(?:nc|sa|nd))*)"
    rf"{_SEPARATOR}*v?(?P<version>\d\.\d)(?!\d)",
    re.IGNORECASE,
)
_ZERO_LICENSE = re.compile(rf"(?<![a-z0-9])cc{_SEPARATOR}?(?:0|zero)(?![0-9a-z])", re.IGNORECASE)
_MARK_LICENSE = re.compile(
    rf"public\s+domain\s+mark|(?<![a-z0-9])pdm{_SEPARATOR}*1\.0(?!\d)", re.IGNORECASE
)
_NON_COMMERCIAL = r"non[\s\-]?commercial|nicht[\s\-]+kommerziell\w*"
_SHARE_ALIKE = r"share[\s\-]?alike|weitergabe\s+unter\s+gleichen\s+bedingungen"
_NO_DERIVATIVES = r"no[\s\-]?derivs|no[\s\-]?derivatives|keine\s+bearbeitung(?:en)?"
_LONG_SEPARATOR = r"[\s\-,\u2010-\u2015]"
_LONG_LICENSE = re.compile(
    rf"\b(?:attribution|namensnennung)"
    rf"(?P<mods>(?:{_LONG_SEPARATOR}*(?:{_NON_COMMERCIAL}|{_SHARE_ALIKE}|{_NO_DERIVATIVES}))*)"
    rf"{_LONG_SEPARATOR}*(?:licen[sc]e\s+|lizenz\s+)?v?(?P<version>\d\.\d)(?!\d)",
    re.IGNORECASE,
)
_NON_COMMERCIAL_WORDS = re.compile(_NON_COMMERCIAL, re.IGNORECASE)
_SHARE_ALIKE_WORDS = re.compile(_SHARE_ALIKE, re.IGNORECASE)
_NO_DERIVATIVES_WORDS = re.compile(_NO_DERIVATIVES, re.IGNORECASE)


def _cc_id(mods: set[str], version: str) -> str | None:
    """``CC-BY-NC-SA-4.0`` from the lower-case modifiers; None for an impossible combination."""
    if version not in _VERSIONS or {"sa", "nd"} <= mods:
        return None
    parts = ["CC", "BY"] + [part.upper() for part in ("nc", "sa", "nd") if part in mods]
    return "-".join([*parts, version])


def _short_mods(text: str) -> set[str]:
    return set(re.findall(r"nc|sa|nd", text.lower()))


def _long_mods(text: str) -> set[str]:
    mods: set[str] = set()
    for key, pattern in (
        ("nc", _NON_COMMERCIAL_WORDS),
        ("sa", _SHARE_ALIKE_WORDS),
        ("nd", _NO_DERIVATIVES_WORDS),
    ):
        if pattern.search(text):
            mods.add(key)
    return mods


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


def _url_license(value: str) -> str | None:
    match = _URL_LICENSE.search(value)
    if not match:
        return None
    if match["public"]:
        return "CC0-1.0" if match["public"].lower() == "zero" else "PDM-1.0"
    return _cc_id(_short_mods(match["kind"][2:]), match["version"])


def normalize_license(value: str) -> str | None:
    """The SPDX-style id of a Creative Commons licence or public-domain tool, or None.

    Accepts creativecommons.org URLs of any version, jurisdiction port and scheme, and the usual
    texts: ``CC BY-SA 4.0``, ``Creative Commons Attribution-ShareAlike 4.0 International``,
    ``Namensnennung - Weitergabe unter gleichen Bedingungen 4.0``, ``CC0``, ``Public Domain
    Mark``. The jurisdiction is dropped (``CC-BY-3.0``, not ``CC-BY-3.0-DE``). A licence without
    a version has no valid id and gives None."""
    license_id = _url_license(value)
    if license_id:
        return license_id
    found = _scan_licenses(value)
    return found[0][0] if found else None


# --- models ------------------------------------------------------------------------------------


class License(BaseModel):
    id: str | None  # SPDX-style, e.g. CC-BY-SA-4.0, CC0-1.0, PDM-1.0
    url: str | None
    label: str | None
    source: LicenseSource
    confidence: Confidence


class Image(BaseModel):
    url: str
    alt: str


class PageMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    authors: list[str] = Field(default_factory=list)
    published: str | None = None
    modified: str | None = None
    site_name: str | None = None
    canonical_url: str | None = None
    language: str | None = None
    keywords: list[str] = Field(default_factory=list)
    license: License | None = None  # the best candidate: highest confidence, then the first
    licenses: list[License] = Field(default_factory=list)
    lrmi: dict[str, Any] = Field(default_factory=dict)
    images: list[Image] = Field(default_factory=list)


# --- language ----------------------------------------------------------------------------------


def _primary_language(value: str | None) -> str | None:
    """``de`` from ``de-DE``, ``en_GB`` or ``fr-CA, en;q=0.5``; None for ``*`` and codes that
    are not a two-letter ISO 639-1 language."""
    if not value:
        return None
    first = value.split(",")[0].split(";")[0].strip().lower().replace("_", "-")
    language = first.split("-")[0]
    return language if re.fullmatch(r"[a-z]{2}", language) else None


@cache
def _identifier() -> LanguageIdentifier:
    return LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def detect_language(html_lang: str | None, header_lang: str | None, text: str) -> str | None:
    """ISO 639-1 language of a page: the ``lang`` attribute, then ``Content-Language``, then
    a guess from the text, which needs at least 50 words to be worth anything."""
    for declared in (html_lang, header_lang):
        language = _primary_language(declared)
        if language:
            return language
    if len(text.split()) < _MIN_DETECTION_WORDS:
        return None
    language, confidence = _identifier().classify(text[:_MAX_DETECTION_CHARS])
    if confidence < _MIN_DETECTION_CONFIDENCE or not re.fullmatch(r"[a-z]{2}", language):
        return None
    return str(language)


# --- generic readers ---------------------------------------------------------------------------


def _clip(value: Any, depth: int = 0) -> Any:
    """The value with every string and list shortened to the limits above."""
    if isinstance(value, str):
        return value[:MAX_FIELD_LENGTH]
    if depth >= _MAX_WALK_DEPTH:
        return None
    if isinstance(value, list):
        return [_clip(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_LIST_ITEMS]
        return {str(key): _clip(item, depth + 1) for key, item in items}
    return value


def _local(key: str) -> str:
    """``license`` from ``license``, ``schema:license`` and ``http://schema.org/license``."""
    return re.split(r"[/#:]", key)[-1]


def _type_names(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type", node.get("type"))
    values = raw if isinstance(raw, list) else [raw]
    return {_local(value) for value in values if isinstance(value, str)}


def _find(node: Any, names: frozenset[str], depth: int = 0) -> Iterator[Any]:
    """The values of the properties with these names, anywhere in the structure except inside
    items of another kind (an image or a comment has its own licence)."""
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            yield from _find(item, names, depth + 1)
    elif isinstance(node, dict):
        if _type_names(node) & _OTHER_ITEM_TYPES:
            return
        for key, value in node.items():
            if isinstance(key, str) and _local(key) in names:
                yield value
            else:
                yield from _find(value, names, depth + 1)


def _plain(value: Any) -> Any:
    """RDFa wraps every literal as ``[{"@value": ...}]``."""
    if (
        isinstance(value, dict)
        and set(value) <= {"@value", "@language", "@type"}
        and "@value" in value
    ):
        return value["@value"]
    if isinstance(value, list):
        items = [_plain(item) for item in value]
        wrapped = all(isinstance(i, dict) and "@value" in i for i in value)
        return items[0] if wrapped and len(items) == 1 else items
    return value


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    """The text of a property value: a string, an object's name or ``@value``, or a list."""
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(value, str):
        if value.strip():
            yield " ".join(value.split())
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, depth + 1)
    elif isinstance(value, dict):
        for key in ("@value", "name", "text", "headline"):
            if key in value:
                yield from _strings(value[key], depth + 1)
                return


def _text(value: Any) -> str | None:
    return next(_strings(value), None)


# --- structured data ---------------------------------------------------------------------------


class _Structured:
    """JSON-LD, microdata and RDFa items of a page; a syntax that fails stays empty."""

    def __init__(self, html: str, url: str) -> None:
        self.json_ld: list[Any] = []
        self.microdata: list[Any] = []
        self.rdfa: list[Any] = []
        data = html.encode()
        try:
            tree = parse_lxml(data, "UTF-8")
        except Exception as error:
            _log.debug("metadata_parse_failed", error=type(error).__name__)
            return
        self.json_ld = self._json_ld(tree)
        self.microdata = self._run("microdata", MicrodataExtractor().extract_items, tree, url)
        if len(html) <= _RDFA_MAX_CHARS and _RDFA_HINT.search(html):
            try:
                rdfa_tree = parse_xmldom_html(data, "UTF-8")
            except Exception as error:
                _log.debug("metadata_parse_failed", error=type(error).__name__)
                return
            self.rdfa = self._run("rdfa", RDFaExtractor().extract_items, rdfa_tree, url)

    @staticmethod
    def _run(syntax: str, extract: Any, tree: Any, url: str) -> list[Any]:
        try:
            return list(extract(tree, base_url=url))
        except Exception as error:  # extruct and its parsers raise anything on odd input
            _log.debug("metadata_syntax_failed", syntax=syntax, error=type(error).__name__)
            return []

    @staticmethod
    def _json_ld(tree: Any) -> list[Any]:
        """One script at a time, so that a broken one does not take the others along."""
        extractor = JsonLdExtractor()
        items: list[Any] = []
        try:
            scripts = list(extractor._xp_jsonld(tree))  # extruct's own script selector
        except Exception as error:
            _log.debug("metadata_syntax_failed", syntax="json-ld", error=type(error).__name__)
            return items
        for script in scripts:
            items.extend(_Structured._run("json-ld", extractor.extract_items, script, ""))
        return items

    def content_nodes(self) -> list[dict[str, Any]]:
        """The JSON-LD nodes that describe the page (``@graph`` members included)."""
        nodes: list[dict[str, Any]] = []

        def add(node: Any, depth: int = 0) -> None:
            if not isinstance(node, dict) or depth > 2:
                return
            graph = node.get("@graph")
            if isinstance(graph, list):
                for member in graph:
                    add(member, depth + 1)
            if not _type_names(node) & _SITE_NODE_TYPES:
                nodes.append(node)

        for item in self.json_ld:
            add(item)
        return nodes

    def site_nodes(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for item in self.json_ld:
            members = item.get("@graph") if isinstance(item, dict) else None
            nodes.extend(
                n for n in (members if isinstance(members, list) else [item]) if isinstance(n, dict)
            )
        return [node for node in nodes if "WebSite" in _type_names(node)]

    def lrmi(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for source in (self.json_ld, self.microdata, self.rdfa):
            for key, value in _walk_keys(source, _LRMI_KEYS):
                cleaned = _clip(_plain(value))
                if key not in found and cleaned not in (None, "", [], {}):
                    found[key] = cleaned
        return found


def _walk_keys(node: Any, names: frozenset[str], depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Like ``_find`` but with the property's local name."""
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk_keys(item, names, depth + 1)
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _local(key) in names:
                yield _local(key), value
            else:
                yield from _walk_keys(value, names, depth + 1)


# --- licences ----------------------------------------------------------------------------------


class _Licenses:
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
        value = " ".join(value.split())
        if not value or len(value) > 2000:
            return
        if is_url is None:
            is_url = bool(re.match(r"(?:https?:)?//\S+$", value))
        label = label[:200] if label else None
        if is_url:
            url = absolute_url(self._base, value)
            if not url or urlsplit(url).scheme not in {"http", "https"}:
                return
            self._found.append(
                License(
                    id=normalize_license(url),
                    url=url,
                    label=label,
                    source=source,
                    confidence=confidence,
                )
            )
        else:
            self._found.append(
                License(
                    id=normalize_license(value),
                    url=None,
                    label=label or value[:200],
                    source=source,
                    confidence=confidence,
                )
            )

    def add_property(self, value: Any, source: LicenseSource, confidence: Confidence) -> None:
        """A structured ``license`` property: a URL, a text or an object with ``@id``/``url``."""
        value = _plain(value)
        if isinstance(value, list):
            for item in value:
                self.add_property(item, source, confidence)
        elif isinstance(value, str):
            self.add(value, source, confidence)
        elif isinstance(value, dict):
            reference = value.get("@id") or value.get("url")
            if isinstance(reference, str):
                self.add(reference, source, confidence, label=_text(value.get("name")))
            else:
                text = _text(value)
                if text:
                    self.add(text, source, confidence)

    def result(self) -> list[License]:
        best: dict[str, License] = {}
        for candidate in self._found:
            key = candidate.id or candidate.url or candidate.label or ""
            known = best.get(key)
            if (
                known is None
                or _CONFIDENCE_RANK[candidate.confidence] < _CONFIDENCE_RANK[known.confidence]
            ):
                best[key] = candidate
        ranked = sorted(
            best.values(),
            key=lambda item: (_CONFIDENCE_RANK[item.confidence], item.id is None),
        )
        return ranked[:_MAX_LICENSES]


def _link_header_licenses(header: str) -> Iterator[str]:
    """Targets of ``Link: <url>; rel="license"``."""
    for match in re.finditer(r"<([^>]*)>([^<]*)", header):
        parameters = match[2]
        rel = re.search(r'rel\s*=\s*(?:"([^"]*)"|([^\s;,]+))', parameters, re.IGNORECASE)
        relations = (rel[1] or rel[2] or "").lower().split() if rel else []
        if "license" in relations:
            yield match[1].strip()


def _is_footer(tag: Tag) -> bool:
    return (
        tag.name == "footer"
        or attr(tag, "role") == "contentinfo"
        or attr(tag, "id") in {"footer", "colophon"}
        or "footer" in attr(tag, "class").split()
    )


def _footers(soup: BeautifulSoup) -> list[Tag]:
    """The page's footers; a CSS selector is far slower than this on big documents."""
    return list(soup.find_all(_is_footer, limit=_MAX_FOOTERS))


# --- DOM ---------------------------------------------------------------------------------------


def _meta_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """(lower-case name or property, content) of every ``<meta>`` in document order."""
    pairs: list[tuple[str, str]] = []
    for tag in soup.find_all("meta"):
        content = " ".join(attr(tag, "content").split())
        if not content:
            continue
        for key in (attr(tag, "property"), attr(tag, "name")):
            if key.strip():
                pairs.append((key.strip().lower(), content))
    return pairs


def _clip_text(value: str | None) -> str | None:
    return value[:MAX_FIELD_LENGTH] if value else None


def _split_keywords(values: list[str]) -> list[str]:
    keywords: list[str] = []
    for value in values:
        for part in value.split(","):
            word = part.strip()
            if word and word not in keywords:
                keywords.append(word[:200])
    return keywords[:_MAX_LIST_ITEMS]


def _http_url(base: str, value: str) -> str | None:
    url = absolute_url(base, value.strip()) if value.strip() else None
    return url if url and urlsplit(url).scheme in {"http", "https"} else None


def _image_urls(value: Any, depth: int = 0) -> Iterator[str]:
    """Image URLs of a JSON-LD ``image`` property: a URL, an object with a URL, or a list."""
    if depth > 3:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _image_urls(item, depth + 1)
    elif isinstance(value, dict):
        yield from _image_urls(value.get("url") or value.get("contentUrl"), depth + 1)


def _images(
    soup: BeautifulSoup,
    base: str,
    meta: list[tuple[str, str]],
    nodes: list[dict[str, Any]],
) -> list[Image]:
    """Cover images first (OpenGraph, Twitter, JSON-LD), then the page's own ``<img>``."""
    found: dict[str, str] = {}

    def add(value: str, alt: str = "") -> str | None:
        url = _http_url(base, value)
        if url and (url in found or len(found) < MAX_IMAGES):
            found.setdefault(url, alt[:MAX_FIELD_LENGTH])
        return url

    last: str | None = None
    for key, content in meta:
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            last = add(content)
        elif key in {"og:image:alt", "twitter:image:alt"} and last in found and not found[last]:
            found[last] = " ".join(content.split())[:MAX_FIELD_LENGTH]
    for node in nodes:
        for source in _image_urls(node.get("image")):
            add(source)
    for tag in soup.find_all("img"):
        if any(attr(tag, size).strip() in {"0", "1"} for size in ("width", "height")):
            continue  # a tracking pixel
        source = next((attr(tag, a).strip() for a in _LAZY_IMAGE_ATTRIBUTES if attr(tag, a)), "")
        if not source.lower().startswith("data:"):
            add(source, " ".join(attr(tag, "alt").split()))
    return [Image(url=url, alt=alt) for url, alt in found.items()]


def _node_values(nodes: list[dict[str, Any]], *keys: str) -> Iterator[str]:
    for key in keys:
        for node in nodes:
            yield from _strings(node.get(key))


def _person_names(nodes: list[dict[str, Any]]) -> Iterator[str]:
    for node in nodes:
        for key in ("author", "creator"):
            yield from _strings(node.get(key))


def _collect_licenses(
    soup: BeautifulSoup,
    structured: _Structured,
    meta: list[tuple[str, str]],
    base: str,
    link_header: str,
) -> list[License]:
    licenses = _Licenses(base)
    for target in _link_header_licenses(link_header):
        licenses.add(target, "rel-license", "high", is_url=True)
    for tag in soup.find_all(["a", "link", "area"], href=True):
        if "license" in attr(tag, "rel").lower().split():
            label = " ".join(tag.get_text().split()) or attr(tag, "title").strip()
            licenses.add(attr(tag, "href"), "rel-license", "high", label=label or None, is_url=True)
    structured_sources: tuple[tuple[LicenseSource, list[Any]], ...] = (
        ("json-ld", structured.json_ld),
        ("microdata", structured.microdata),
        ("rdfa", structured.rdfa),
    )
    for source, items in structured_sources:
        for value in _find(items, frozenset({"license"})):
            licenses.add_property(value, source, "high")
    for value in _find(structured.rdfa, frozenset({"rights"})):
        licenses.add_property(value, "rdfa", "medium")
    for key, content in meta:
        if key in _LICENSE_META_NAMES:
            licenses.add(content, "meta", "medium")
        elif key in _OPENGRAPH_LICENSE_NAMES:
            licenses.add(content, "opengraph", "medium")
    for footer in _footers(soup):
        for anchor in footer.find_all("a", href=True):
            href = attr(anchor, "href")
            if normalize_license(href):
                label = " ".join(anchor.get_text().split())
                licenses.add(href, "text", "low", label=label or None, is_url=True)
        for _, snippet in _scan_licenses(footer.get_text(" ", strip=True)[:_MAX_FOOTER_CHARS]):
            licenses.add(snippet, "text", "low")
    return licenses.result()


def _first_of(*groups: Iterable[str]) -> str | None:
    """The first non-empty text of the groups, tried in order, clipped."""
    for group in groups:
        for value in group:
            if value.strip():
                return value[:MAX_FIELD_LENGTH]
    return None


def _publishers(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n["publisher"] for n in nodes if isinstance(n.get("publisher"), dict)]


def extract_metadata(html: str, url: str, headers: Mapping[str, str]) -> PageMetadata:
    """The metadata a page declares. ``url`` is the final URL (relative references resolve
    against it, or against ``<base href>``); ``headers`` are the response headers.

    Never raises on bad markup: a source that cannot be read is left out."""
    try:
        soup = parse_html(html)
    except Exception as error:  # lxml and BeautifulSoup raise anything on hostile input
        _log.debug("metadata_parse_failed", error=type(error).__name__)
        return PageMetadata()
    lowered = {name.lower(): value for name, value in headers.items()}
    base = base_url_of(soup, url)
    structured = _Structured(html, base)
    nodes = structured.content_nodes()
    pairs = _meta_pairs(soup)
    index: dict[str, list[str]] = {}
    for key, content in pairs:
        index.setdefault(key, []).append(content)

    def declared(*keys: str) -> Iterator[str]:
        for key in keys:
            yield from index.get(key, [])

    title_tag = soup.find("title")
    html_tag = soup.find("html")
    licenses = _collect_licenses(soup, structured, pairs, base, lowered.get("link", ""))
    canonicals = (
        attr(tag, "href")
        for tag in soup.find_all("link", href=True)
        if "canonical" in attr(tag, "rel").lower().split()
    )
    declared_languages = (
        attr(html_tag, "lang") if isinstance(html_tag, Tag) else "",
        lowered.get("content-language", ""),
        *declared("og:locale"),
        *_node_values(nodes, "inLanguage"),
    )
    authors = [
        name
        for name in [*declared(*_AUTHOR_META), *_node_values(nodes, "author", "creator")]
        if not re.match(r"https?://", name)  # article:author is often a profile URL
    ]
    return PageMetadata(
        title=_first_of(
            declared("og:title", "twitter:title"),
            _node_values(nodes, "headline", "name"),
            [" ".join(title_tag.get_text().split()) if title_tag else ""],
        ),
        description=_first_of(
            declared("og:description", "description", "twitter:description"),
            _node_values(nodes, "description"),
        ),
        authors=list(dict.fromkeys(authors))[:_MAX_LIST_ITEMS],
        published=_first_of(declared(*_PUBLISHED_META), _node_values(nodes, "datePublished")),
        modified=_first_of(declared(*_MODIFIED_META), _node_values(nodes, "dateModified")),
        site_name=_first_of(
            declared("og:site_name"),
            _node_values(structured.site_nodes(), "name"),
            _node_values(_publishers(nodes), "name"),
            declared("application-name"),
        ),
        canonical_url=next(
            filter(None, (_http_url(base, value) for value in [*canonicals, *declared("og:url")])),
            None,
        ),
        language=next(filter(None, map(_primary_language, declared_languages)), None),
        keywords=_split_keywords(
            [
                *declared("keywords", "article:tag", "news_keywords"),
                *_node_values(nodes, "keywords"),
            ]
        ),
        license=licenses[0] if licenses else None,
        licenses=licenses,
        lrmi=structured.lrmi(),
        images=_images(soup, base, pairs, nodes),
    )
