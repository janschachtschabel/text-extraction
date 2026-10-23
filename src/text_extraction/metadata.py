"""What a page says about itself: title, authors, dates, licence, LRMI fields, images, language.

Everything here is read from the markup the page already carries, on one lxml tree: JSON-LD,
microdata and RDFa through extruct, OpenGraph, meta tags, links and images by XPath. Each source
can be broken or hostile, so a failing reader costs the page that one source and nothing else.
Licences are ranked in ``text_extraction.licenses``.

Structured data is read from the page's own items only: top-level items, ``@graph`` members and
``mainEntity``. What hangs off them (list entries, parts, sources, media) describes other things.

``extract_metadata`` is synchronous and CPU-bound: callers run it in a thread.
"""

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from itertools import chain, islice
from typing import Any
from urllib.parse import urlsplit

import jstyleson
import structlog
from extruct.rdfa import RDFaExtractor
from extruct.utils import parse_xmldom_html
from extruct.w3cmicrodata import MicrodataExtractor
from lxml.html import HtmlElement, HTMLParser, fromstring
from py3langid.langid import MODEL_FILE, LanguageIdentifier
from pydantic import BaseModel, Field

from text_extraction.convert.links import absolute_url
from text_extraction.licenses import (
    License,
    LicenseCollector,
    LicenseSource,
    link_header_licenses,
)

_log = structlog.get_logger(__name__)

MAX_FIELD_LENGTH = 1000  # page-controlled text: keeps metadata from carrying a second document
MAX_IMAGES = 20
MAX_KEYWORDS = 50
_MAX_LIST_ITEMS = 50
_MAX_ITEMS_SCANNED = 200  # values of one list property that are looked at
_MAX_NODES = 200
_MAX_WALK_DEPTH = 12
_MAX_FOOTER_CHARS = 5000
_MAX_FOOTERS = 3  # the last ones: the site footer, not the footers of articles before it
_MAX_FOOTER_ANCHORS = 100
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

# Items that carry their own licence and details: a picture, a comment, a person.
_OTHER_ITEM_TYPES = frozenset(
    {
        "ImageObject",
        "VideoObject",
        "AudioObject",
        "MediaObject",
        "Photograph",
        "Comment",
        "Review",
        "Person",
    }
)
# Nodes that describe the site or the publisher rather than this page.
_SITE_NODE_TYPES = frozenset(
    {
        "WebSite",
        "Organization",
        "EducationalOrganization",
        "Corporation",
        "LocalBusiness",
        "BreadcrumbList",
        "ListItem",
        "SiteNavigationElement",
    }
)
_LRMI_KEYS = (
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
)
_LICENSE_META_NAMES = frozenset(
    {"license", "licence", "dc.license", "dcterms.license", "dc.rights", "dcterms.rights", "rights"}
)
_OPENGRAPH_LICENSE_NAMES = frozenset({"og:license", "article:license"})
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
_LAZY_IMAGE_ATTRIBUTES = ("src", "data-src", "data-original", "data-lazy-src")
# ``contains`` finds candidates cheaply; whole class tokens are checked in ``_footers``.
_FOOTER_XPATH = (
    "//footer | //*[@role='contentinfo'] | //*[@id='footer' or @id='colophon']"
    " | //*[contains(@class, 'footer')]"
)
_JSON_LD_TYPE = "application/ld+json"
_JSON_LD_WRAPPERS = re.compile(
    r"^\s*(?:<!--|//\s*<!\[CDATA\[|/\*\s*<!\[CDATA\[\s*\*/)"
    r"|(?:-->|//\s*\]\]>|/\*\s*\]\]>\s*\*/)\s*$"
)

# --- models ------------------------------------------------------------------------------------


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
    license: License | None = None  # the best candidate: see LicenseCollector
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
    a guess from the start of the text, which needs at least 50 words to be worth anything."""
    for declared in (html_lang, header_lang):
        language = _primary_language(declared)
        if language:
            return language
    sample = text[:_MAX_DETECTION_CHARS]
    if len(sample.split()) < _MIN_DETECTION_WORDS:
        return None
    language, confidence = _identifier().classify(sample)
    if confidence < _MIN_DETECTION_CONFIDENCE or not re.fullmatch(r"[a-z]{2}", language):
        return None
    return str(language)


# --- structured data ---------------------------------------------------------------------------


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


def _plain(value: Any) -> Any:
    """RDFa wraps every literal as ``[{"@value": ...}]``."""
    if (
        isinstance(value, dict)
        and "@value" in value
        and set(value) <= {"@value", "@language", "@type"}
    ):
        return value["@value"]
    if isinstance(value, list):
        items = [_plain(item) for item in value[:_MAX_LIST_ITEMS]]
        wrapped = all(isinstance(i, dict) and "@value" in i for i in value[:_MAX_LIST_ITEMS])
        return items[0] if wrapped and len(items) == 1 else items
    return value


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    """The text of a property value: a string, an object's name or ``@value``, or a list."""
    if depth > 3:
        return
    if isinstance(value, str):
        if value.strip():
            yield " ".join(value.split())
    elif isinstance(value, list):
        for item in value[:_MAX_ITEMS_SCANNED]:
            yield from _strings(item, depth + 1)
    elif isinstance(value, dict):
        for key in ("@value", "name", "text", "headline"):
            if key in value:
                yield from _strings(value[key], depth + 1)
                return


def _local(key: str) -> str:
    """``license`` from ``license``, ``schema:license`` and ``http://schema.org/license``."""
    return re.split(r"[/#:]", key)[-1]


def _node(raw: dict[Any, Any]) -> dict[str, Any]:
    """A JSON-LD, microdata or RDFa item with plain property names (``license``, not
    ``http://schema.org/license``); a microdata item's ``properties`` become its keys."""
    if isinstance(raw.get("properties"), dict) and "@type" not in raw:
        raw = {"@type": raw.get("type"), **raw["properties"]}
    node: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and _local(key):
            node.setdefault(_local(key), value)
    return node


def _type_names(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {_local(value) for value in values if isinstance(value, str)}


def _own_nodes(items: list[Any]) -> list[dict[str, Any]]:
    """The items that describe the page itself: the top-level ones, the members of their
    ``@graph`` and their ``mainEntity``. A list page's entries, a work the page is based on,
    its parts and its media are left out, as are items of another kind (picture, person)."""
    nodes: list[dict[str, Any]] = []

    def add(raw: Any, depth: int) -> None:
        if not isinstance(raw, dict) or depth > 3 or len(nodes) >= _MAX_NODES:
            return
        node = _node(raw)
        if _type_names(node) & _OTHER_ITEM_TYPES:
            return
        nodes.append(node)
        for key in ("@graph", "mainEntity"):
            members = node.get(key)
            for member in members[:_MAX_NODES] if isinstance(members, list) else [members]:
                add(member, depth + 1)

    for item in items[:_MAX_NODES]:
        add(item, 0)
    return nodes


def _json_ld_items(tree: HtmlElement) -> list[Any]:
    """The items of every JSON-LD script, one script at a time, so that a broken one does not
    take the others along. The script type is matched like browsers do: case-insensitively and
    ignoring parameters (``; charset=utf-8``)."""
    items: list[Any] = []
    for script in tree.iter("script"):
        if (script.get("type") or "").split(";")[0].strip().lower() != _JSON_LD_TYPE:
            continue
        text = script.text or ""
        try:
            try:
                data = json.loads(text, strict=False)
            except ValueError:
                # Lenient JSON, as many CMS templates write it: comment or CDATA markers around
                # the script, `//` comments, trailing commas.
                data = jstyleson.loads(_JSON_LD_WRAPPERS.sub("", text), strict=False)
        except (ValueError, RecursionError) as error:
            _log.debug("metadata_syntax_failed", syntax="json-ld", error=type(error).__name__)
            continue
        items.extend(data if isinstance(data, list) else [data])
    return [item for item in items if isinstance(item, dict)]


def _run(syntax: str, extract: Any, tree: Any, url: str) -> list[Any]:
    try:
        return list(extract(tree, base_url=url))
    except Exception as error:  # extruct and its parsers raise anything on odd input
        _log.debug("metadata_syntax_failed", syntax=syntax, error=type(error).__name__)
        return []


class _Structured:
    """The page's own JSON-LD, microdata and RDFa nodes; a syntax that fails stays empty."""

    def __init__(self, tree: HtmlElement, html: str, base: str) -> None:
        self.json_ld = _own_nodes(_json_ld_items(tree))
        self.microdata = _own_nodes(
            _run("microdata", MicrodataExtractor().extract_items, tree, base)
        )
        self.rdfa: list[dict[str, Any]] = []
        if len(html) <= _RDFA_MAX_CHARS and _RDFA_HINT.search(html):
            try:
                rdfa_tree = parse_xmldom_html(html.encode(), "UTF-8")
            except Exception as error:
                _log.debug("metadata_parse_failed", error=type(error).__name__)
            else:
                self.rdfa = _own_nodes(_run("rdfa", RDFaExtractor().extract_items, rdfa_tree, base))

    def content_nodes(self) -> list[dict[str, Any]]:
        """The JSON-LD nodes that describe the page rather than its site."""
        return [n for n in self.json_ld if not _type_names(n) & _SITE_NODE_TYPES]

    def site_nodes(self) -> list[dict[str, Any]]:
        return [n for n in self.json_ld if "WebSite" in _type_names(n)]

    def lrmi(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for nodes in (self.json_ld, self.microdata, self.rdfa):
            for node in nodes:
                for key in _LRMI_KEYS:
                    if key not in found and key in node:
                        cleaned = _clip(_plain(node[key]))
                        if cleaned not in (None, "", [], {}):
                            found[key] = cleaned
        return found


def _node_values(nodes: Iterable[dict[str, Any]], *keys: str) -> Iterator[str]:
    """The texts of these properties of the nodes, by key first, then by node."""
    nodes = list(nodes)
    for key in keys:
        for node in nodes:
            yield from _strings(node.get(key))


# --- DOM ---------------------------------------------------------------------------------------


def _squash(text: str | None) -> str:
    return " ".join((text or "").split())


def _bounded_text(element: HtmlElement, limit: int) -> str:
    """The first ``limit`` characters of an element's text, without reading all of it."""
    parts: list[str] = []
    size = 0
    for piece in element.itertext():
        parts.append(piece)
        size += len(piece)
        if size >= limit:
            break
    return _squash(" ".join(parts))[:limit]


def _meta_pairs(tree: HtmlElement) -> list[tuple[str, str]]:
    """(lower-case name or property, content) of every ``<meta>`` in document order."""
    pairs: list[tuple[str, str]] = []
    for tag in tree.iter("meta"):
        content = _squash(tag.get("content"))
        if not content:
            continue
        for key in (tag.get("property"), tag.get("name")):
            if key and key.strip():
                pairs.append((key.strip().lower(), content))
    return pairs


def _base_url(tree: HtmlElement, url: str) -> str:
    """The URL that relative references resolve against: ``<base href>`` when it is usable."""
    for tag in tree.iter("base"):
        href = (tag.get("href") or "").strip()
        if href:
            return absolute_url(url, href) or url
    return url


def _http_url(base: str, value: str) -> str | None:
    value = value.strip()
    url = absolute_url(base, value) if value else None
    return url if url and urlsplit(url).scheme in {"http", "https"} else None


def _split_keywords(values: Iterable[str]) -> list[str]:
    """Distinct keywords from comma-separated texts, at most 50; stops reading after that."""
    seen: dict[str, None] = {}
    for value in values:
        for part in value.split(","):
            word = part.strip()
            if word and len(seen) < MAX_KEYWORDS:
                seen.setdefault(word[:200])
        if len(seen) >= MAX_KEYWORDS:
            break
    return list(seen)


def _image_urls(value: Any, depth: int = 0) -> Iterator[str]:
    """Image URLs of a JSON-LD ``image`` property: a URL, an object with a URL, or a list."""
    if depth > 3:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value[:_MAX_ITEMS_SCANNED]:
            yield from _image_urls(item, depth + 1)
    elif isinstance(value, dict):
        yield from _image_urls(value.get("url") or value.get("contentUrl"), depth + 1)


def _images(
    tree: HtmlElement, base: str, meta: list[tuple[str, str]], nodes: list[dict[str, Any]]
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
            found[last] = content[:MAX_FIELD_LENGTH]
    for node in nodes:
        for source in _image_urls(node.get("image")):
            add(source)
    for tag in tree.iter("img"):
        if len(found) >= MAX_IMAGES:
            break
        if any((tag.get(size) or "").strip() in {"0", "1"} for size in ("width", "height")):
            continue  # a tracking pixel
        source = next((tag.get(a, "").strip() for a in _LAZY_IMAGE_ATTRIBUTES if tag.get(a)), "")
        if not source.lower().startswith("data:"):
            add(source, _squash(tag.get("alt")))
    return [Image(url=url, alt=alt) for url, alt in found.items()]


def _footers(tree: HtmlElement) -> list[HtmlElement]:
    """The last few footers of the page, in document order: the site footer, not the footers of
    the articles before it."""
    found = [
        element
        for element in tree.xpath(_FOOTER_XPATH)
        if element.tag == "footer"
        or element.get("role") == "contentinfo"
        or element.get("id") in {"footer", "colophon"}
        or "footer" in (element.get("class") or "").split()
    ]
    return found[-_MAX_FOOTERS:]


def _collect_licenses(
    tree: HtmlElement,
    structured: _Structured,
    meta: list[tuple[str, str]],
    base: str,
    link_header: str,
) -> list[License]:
    licenses = LicenseCollector(base)
    for target in link_header_licenses(link_header):
        licenses.add(target, "rel-license", "high", is_url=True)
    for tag in tree.iter("a", "link", "area"):
        href = tag.get("href")
        if href and "license" in (tag.get("rel") or "").lower().split():
            label = _squash(tag.text_content()) or _squash(tag.get("title"))
            # A licence link in a figure credits the picture, not the page.
            credit = bool(tag.xpath("ancestor::figure | ancestor::figcaption"))
            licenses.add(
                href,
                "rel-license",
                "medium" if credit else "high",
                label=label or None,
                is_url=True,
            )
    sources: tuple[tuple[LicenseSource, list[dict[str, Any]]], ...] = (
        ("json-ld", structured.json_ld),
        ("microdata", structured.microdata),
        ("rdfa", structured.rdfa),
    )
    for source, nodes in sources:
        for node in nodes:
            if "license" in node:
                licenses.add_property(node["license"], source, "high")
    for node in structured.rdfa:
        if "rights" in node:
            licenses.add_property(node["rights"], "rdfa", "medium")
    for key, content in meta:
        if key in _LICENSE_META_NAMES:
            licenses.add(content, "meta", "medium")
        elif key in _OPENGRAPH_LICENSE_NAMES:
            licenses.add(content, "opengraph", "medium")
    for footer in _footers(tree):
        anchors = [
            (anchor.get("href", "").strip(), _squash(anchor.text_content()))
            for anchor in islice(footer.iter("a"), _MAX_FOOTER_ANCHORS)
            if anchor.get("href")
        ]
        licenses.add_footer(_bounded_text(footer, _MAX_FOOTER_CHARS), anchors)
    return licenses.result()


def _distinct(values: Iterable[str], count: int) -> list[str]:
    """The first ``count`` different values; stops reading once it has them."""
    found: dict[str, None] = {}
    for value in values:
        found.setdefault(value)
        if len(found) >= count:
            break
    return list(found)


def _first_of(*groups: Iterable[str]) -> str | None:
    """The first non-empty text of the groups, tried in order, clipped."""
    for group in groups:
        for value in group:
            if value.strip():
                return value[:MAX_FIELD_LENGTH]
    return None


def _publishers(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_node(n["publisher"]) for n in nodes if isinstance(n.get("publisher"), dict)]


def _parse(html: str) -> HtmlElement:
    """The page as an lxml tree; an empty document when it cannot be parsed. Bytes, because
    lxml rejects a str that carries an XML encoding declaration. ``huge_tree`` lifts libxml2's
    limit of 256 nested elements, which would cut a deeply nested page off, footer and all."""
    try:
        return fromstring(html.encode(), parser=HTMLParser(encoding="utf-8", huge_tree=True))
    except Exception as error:  # an empty document, or lxml giving up
        _log.debug("metadata_parse_failed", error=type(error).__name__)
        return fromstring(b"<html></html>", parser=HTMLParser(encoding="utf-8"))


def extract_metadata(html: str, url: str, headers: Mapping[str, str]) -> PageMetadata:
    """The metadata a page declares. ``url`` is the final URL (relative references resolve
    against it, or against ``<base href>``); ``headers`` are the response headers.

    Never raises on bad markup: a source that cannot be read is left out."""
    tree = _parse(html)
    lowered = {name.lower(): value for name, value in headers.items()}
    base = _base_url(tree, url)
    structured = _Structured(tree, html, base)
    nodes = structured.content_nodes()
    pairs = _meta_pairs(tree)
    index: dict[str, list[str]] = {}
    for key, content in pairs:
        index.setdefault(key, []).append(content)

    def declared(*keys: str) -> Iterator[str]:
        for key in keys:
            yield from index.get(key, [])

    title_tag = next(tree.iter("title"), None)
    root = tree.getroottree().getroot()
    licenses = _collect_licenses(tree, structured, pairs, base, lowered.get("link", ""))
    canonicals = (
        (tag.get("href") or "")
        for tag in tree.iter("link")
        if "canonical" in (tag.get("rel") or "").lower().split()
    )
    declared_languages = chain(
        [
            (root.get("lang") or "") if root.tag == "html" else "",
            lowered.get("content-language", ""),
        ],
        declared("og:locale"),
        _node_values(nodes, "inLanguage"),
    )
    authors = (
        name
        for name in chain(declared(*_AUTHOR_META), _node_values(nodes, "author", "creator"))
        if not re.match(r"https?://", name)  # article:author is often a profile URL
    )
    return PageMetadata(
        title=_first_of(
            declared("og:title", "twitter:title"),
            _node_values(nodes, "headline", "name"),
            [_squash(title_tag.text_content()) if title_tag is not None else ""],
        ),
        description=_first_of(
            declared("og:description", "description", "twitter:description"),
            _node_values(nodes, "description"),
        ),
        authors=_distinct(authors, _MAX_LIST_ITEMS),
        published=_first_of(declared(*_PUBLISHED_META), _node_values(nodes, "datePublished")),
        modified=_first_of(declared(*_MODIFIED_META), _node_values(nodes, "dateModified")),
        site_name=_first_of(
            declared("og:site_name"),
            _node_values(structured.site_nodes(), "name"),
            _node_values(_publishers(nodes), "name"),
            declared("application-name"),
        ),
        canonical_url=next(
            filter(
                None, (_http_url(base, value) for value in chain(canonicals, declared("og:url")))
            ),
            None,
        ),
        language=next(filter(None, map(_primary_language, declared_languages)), None),
        keywords=_split_keywords(
            chain(
                declared("keywords", "article:tag", "news_keywords"),
                _node_values(nodes, "keywords"),
            )
        ),
        license=licenses[0] if licenses else None,
        licenses=licenses,
        lrmi=structured.lrmi(),
        images=_images(tree, base, pairs, nodes),
    )
