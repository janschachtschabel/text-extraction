"""Page types and link categories by rules (package 14): no I/O, no model, no network.

``url_type`` reads what a URL's path and query say about the page (an imprint, a login, a search
result, a listing, the home page). ``link_category`` puts a link of a page into a category for its
reader: ``url_type`` for a link inside the site, the host for a social network, the file kind for
a download, the link text for a navigation helper. ``page_type`` classifies a fetched page from
its signals, strongest first (ruling P14-S4):

1. ``error``: the status is 400 or higher, or the quality gate saw a soft 404.
2. ``home``: the path is empty, ``/`` or ``/index.html``.
3. schema.org: the JSON-LD ``@type`` (top-level nodes and the ``@graph``) or the ``itemtype`` of
   the outermost microdata scope says ``ContactPage``, ``AboutPage``, ``SearchResultsPage`` or
   ``CollectionPage``.
4. ``url_type`` (imprint, privacy, legal, auth, search, contact, about, listing).
5. The ``<title>`` or the first ``<h1>`` starts with the name of a type (``Impressum``,
   ``Kontakt``, ``Login`` ...), followed by nothing or a separator.
6. ``auth``: a password input on a thin page.
7. ``listing``: ``rel="next|prev"``, or a page that is mostly links.
8. ``content`` when the extracted content has enough words, else ``other``.

``PageTypeInfo.signals`` lists every signal that agrees with the winner, also those that rank
lower, in the order of ``PageSignal``. ``og_type`` (``og:type`` is ``article``) only ever agrees
with ``content``; it never decides.

The page is untrusted input: JSON-LD is parsed up to a size and a count, malformed or deeply
nested JSON is ignored, and nothing here raises on bad HTML or a bad URL.

This module imports only the standard library, lxml and pydantic at run time (``Quality`` is
imported for typing only): ``convert.links`` imports ``LinkCategory`` from here, and
``quality`` imports ``convert.markup``.
"""

import json
import re
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from lxml.html import HtmlElement
from pydantic import BaseModel

from text_extraction.structured.rules import parse_page

if TYPE_CHECKING:
    from text_extraction.quality import Quality

PageType = Literal[
    "home",
    "content",
    "listing",
    "imprint",
    "privacy",
    "legal",
    "contact",
    "about",
    "auth",
    "search",
    "error",
    "other",
]
LinkCategory = Literal[
    "home",
    "content",
    "listing",
    "imprint",
    "privacy",
    "legal",
    "contact",
    "about",
    "auth",
    "search",
    "social",
    "download",
    "nav",
]
PageSignal = Literal[
    "status",
    "soft_404",
    "root_path",
    "schema_org",
    "og_type",
    "url",
    "title",
    "h1",
    "password_field",
    "pagination",
    "link_density",
    "word_count",
]
LinkKind = Literal["internal", "external", "file"]

# The share of the visible characters that sits in links, and the number of links, from which a
# page is a listing (an index of other pages) and not an article.
LISTING_LINK_SHARE: Final = 0.6
LISTING_MIN_LINKS: Final = 20

_MAX_JSON_LD_CHARS: Final = 500_000  # per script; a larger one is not parsed
_MAX_JSON_LD_SCRIPTS: Final = 20
_MAX_JSON_LD_NODES: Final = 200  # nodes of one script (top level and ``@graph``)
_MAX_MICRODATA_SCOPES: Final = 50
_MAX_HEADING_CHARS: Final = 300

# The order in which several types of one URL are reported.
_URL_ORDER: Final[tuple[PageType, ...]] = (
    "imprint",
    "privacy",
    "legal",
    "auth",
    "search",
    "contact",
    "about",
    "listing",
)


def _fold(text: str) -> str:
    """Case-folded, umlauts as ``ae oe ue``, whitespace collapsed."""
    folded = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return " ".join(folded.split())


_SEGMENT_NAMES: Final[dict[PageType, tuple[str, ...]]] = {
    "imprint": ("impressum", "imprint", "legal-notice", "site-notice", "anbieterkennzeichnung"),
    "privacy": (
        "datenschutz",
        "datenschutzerklaerung",
        "datenschutzerklärung",
        "datenschutzhinweise",
        "privacy",
        "privacy-policy",
        "privacy-notice",
        "dsgvo",
        "gdpr",
    ),
    "legal": (
        "agb",
        "terms",
        "terms-of-service",
        "terms-of-use",
        "nutzungsbedingungen",
        "cookies",
        "cookie-policy",
        "cookie-richtlinie",
        "disclaimer",
        "haftungsausschluss",
        "rechtliches",
        "legal",
        "widerruf",
        "widerrufsrecht",
        "barrierefreiheit",
        "barrierefreiheitserklaerung",
        "erklaerung-zur-barrierefreiheit",
        "accessibility",
        "accessibility-statement",
    ),
    "contact": ("kontakt", "contact", "contact-us", "kontaktformular"),
    "about": (
        "ueber-uns",
        "uber-uns",
        "über-uns",
        "about",
        "about-us",
        "wir-ueber-uns",
        "team",
        "wer-wir-sind",
        "who-we-are",
    ),
    "auth": (
        "login",
        "logout",
        "signin",
        "sign-in",
        "signout",
        "sign-out",
        "signup",
        "sign-up",
        "register",
        "registrieren",
        "registrierung",
        "anmelden",
        "anmeldung",
        "abmelden",
        "account",
        "my-account",
        "mein-konto",
        "konto",
        "profil",
        "profile",
        "passwort",
        "password",
        "password-reset",
    ),
    "search": ("search", "suche", "recherche"),
    "listing": (
        "kategorie",
        "category",
        "categories",
        "tag",
        "tags",
        "schlagwort",
        "archiv",
        "archive",
    ),
}
_SEGMENT_TYPE: Final[dict[str, PageType]] = {
    _fold(name): page_type_name
    for page_type_name, names in _SEGMENT_NAMES.items()
    for name in names
}
_SEARCH_PARAMETERS: Final = frozenset({"q", "query", "search", "suche", "s", "keyword"})
_PAGE_PARAMETERS: Final = frozenset({"page", "seite"})
_PAGE_SEGMENTS: Final = frozenset({"page", "seite"})
_FILE_EXTENSION = re.compile(r"\.(?:html?|php|aspx?)$")
_HOME_PATH = re.compile(r"^/?(?:index\.(?:html?|php))?$", re.IGNORECASE)


def _segment_stem(segment: str) -> str:
    return _FILE_EXTENSION.sub("", _fold(segment)).replace("_", "-")


def url_type(url: str) -> PageType | None:
    """The type that the path and the query of ``url`` say, else None.

    A path segment (or its stem before ``.html``, ``.htm``, ``.php``) has to be a name in full:
    ``/impressum.html`` is an imprint, ``/impressum-archiv-2019`` is not. Home is an empty path,
    ``/`` or ``/index.html`` without a query that says something else.
    """
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query)
    except ValueError:
        return None
    path = unquote(parts.path)
    segments = [segment for segment in path.split("/") if segment]
    stems = [_segment_stem(segment) for segment in segments]

    found: set[PageType] = {_SEGMENT_TYPE[stem] for stem in stems if stem in _SEGMENT_TYPE}
    for index, stem in enumerate(stems[:-1]):
        if stem in _PAGE_SEGMENTS and stems[index + 1].isdecimal():
            found.add("listing")
    for key, value in query:
        name = key.lower()
        if name in _SEARCH_PARAMETERS and value.strip():
            found.add("search")
        elif name in _PAGE_PARAMETERS and value.strip().isdecimal():
            found.add("listing")
    for candidate in _URL_ORDER:
        if candidate in found:
            return candidate
    if _HOME_PATH.match(path):
        return "home"
    return None


_SOCIAL_HOSTS: Final = (
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "xing.com",
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "tiktok.com",
    "pinterest.com",
    "mastodon.social",
    "bsky.app",
    "threads.net",
    "t.me",
    "telegram.me",
    "whatsapp.com",
    "wa.me",
    "reddit.com",
    "github.com",
    "flickr.com",
    "soundcloud.com",
    "twitch.tv",
    "discord.com",
    "discord.gg",
)
_NAV_TEXTS: Final = frozenset(
    {
        "home",
        "start",
        "startseite",
        "nach oben",
        "back to top",
        "top",
        "menu",
        "menü",
        "navigation",
        "zurück",
        "back",
        "übersicht",
        "overview",
        "sitemap",
        "→",
        "←",
        "›",  # noqa: RUF001 - a navigation arrow
        "‹",  # noqa: RUF001 - a navigation arrow
        "»",
        "«",
        "▲",
    }
)


_URL_LINK_CATEGORY: Final[dict[PageType, LinkCategory]] = {
    "home": "home",
    "listing": "listing",
    "imprint": "imprint",
    "privacy": "privacy",
    "legal": "legal",
    "contact": "contact",
    "about": "about",
    "auth": "auth",
    "search": "search",
}


def _is_social(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == social or host.endswith(f".{social}") for social in _SOCIAL_HOSTS)


def link_category(url: str, text: str, kind: LinkKind) -> LinkCategory:
    """The category of a link: a file is a ``download``; an external link is ``social`` or
    ``content`` (another site's root or imprint says nothing about this one); an internal link is
    what ``url_type`` says, else ``nav`` when its text is a navigation helper, else ``content``."""
    if kind == "file":
        return "download"
    if kind == "external":
        return "social" if _is_social(url) else "content"
    by_url = url_type(url)
    if by_url is not None and by_url in _URL_LINK_CATEGORY:
        return _URL_LINK_CATEGORY[by_url]
    if " ".join(text.split()).casefold() in _NAV_TEXTS:
        return "nav"
    return "content"


# --- page_type ----------------------------------------------------------------------------------

_HEADING_WORDS: Final[dict[PageType, tuple[str, ...]]] = {
    "imprint": ("Impressum", "Imprint"),
    "privacy": ("Datenschutz", "Datenschutzerklärung", "Privacy Policy"),
    "legal": ("AGB", "Nutzungsbedingungen", "Terms of Use"),
    "contact": ("Kontakt", "Contact"),
    "about": ("Über uns", "About us"),
    "auth": ("Anmelden", "Login", "Registrieren"),
    "search": ("Suche", "Search results", "Suchergebnisse"),
}
_HEADING_TYPE: Final[dict[str, PageType]] = {
    _fold(word): page_type_name
    for page_type_name, words in _HEADING_WORDS.items()
    for word in words
}
# The name, then nothing or a separator (a hyphen only with a space before or after it).
_HEADING = re.compile(
    r"^(?P<word>"
    + "|".join(re.escape(word) for word in sorted(_HEADING_TYPE, key=len, reverse=True))
    + r")(?=\s*$|\s*[|\u2013\u2014:\u00b7]|\s+-|-\s)"
)

_SCHEMA_TYPES: Final[tuple[tuple[str, PageType], ...]] = (
    ("ContactPage", "contact"),
    ("AboutPage", "about"),
    ("SearchResultsPage", "search"),
    ("CollectionPage", "listing"),
)
_NON_VISIBLE: Final = ("script", "style", "noscript", "template")


class PageTypeInfo(BaseModel):
    """The type of a page and the signals that agree with it."""

    type: PageType
    signals: list[PageSignal]


def _schema_name(value: str) -> str:
    return re.split(r"[/#:]", value.strip().rstrip("/"))[-1]


def _json_ld_types(node: object) -> list[str]:
    if not isinstance(node, dict):
        return []
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [_schema_name(value) for value in values if isinstance(value, str)]


def _json_ld_nodes(data: object) -> list[object]:
    """The top-level nodes of a JSON-LD document and the nodes of its ``@graph``."""
    nodes: list[object] = []
    top = data if isinstance(data, list) else [data]
    for item in top[:_MAX_JSON_LD_NODES]:
        nodes.append(item)
        if isinstance(item, dict) and isinstance(item.get("@graph"), list):
            nodes.extend(item["@graph"][:_MAX_JSON_LD_NODES])
    return nodes


def _schema_type_names(tree: HtmlElement) -> set[str]:
    names: set[str] = set()
    scripts = 0
    for script in tree.iter("script"):
        if not (script.get("type") or "").strip().lower().startswith("application/ld+json"):
            continue
        scripts += 1
        if scripts > _MAX_JSON_LD_SCRIPTS:
            break
        text = script.text or ""
        if len(text) > _MAX_JSON_LD_CHARS:
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        for node in _json_ld_nodes(data):
            names.update(_json_ld_types(node))
    try:
        scopes = tree.xpath("//*[@itemscope][not(ancestor::*[@itemscope])]")
    except Exception:  # an xpath that lxml gives up on is no signal
        scopes = []
    for scope in scopes[:_MAX_MICRODATA_SCOPES]:
        names.update(_schema_name(item) for item in (scope.get("itemtype") or "").split())
    return names


def _schema_page_type(tree: HtmlElement) -> PageType | None:
    names = _schema_type_names(tree)
    for name, found in _SCHEMA_TYPES:
        if name in names:
            return found
    return None


def _heading_type(text: str | None) -> PageType | None:
    match = _HEADING.match(_fold((text or "")[:_MAX_HEADING_CHARS]))
    return _HEADING_TYPE[match.group("word")] if match else None


def _has_og_article(tree: HtmlElement) -> bool:
    for meta in tree.iter("meta"):
        if (meta.get("property") or "").strip().lower() == "og:type":
            return (meta.get("content") or "").strip().lower() == "article"
    return False


def _has_pagination(tree: HtmlElement) -> bool:
    for element in tree.iter("link", "a"):
        if {"next", "prev"} & set((element.get("rel") or "").lower().split()):
            return True
    return False


def _has_password_input(tree: HtmlElement) -> bool:
    return any(
        (field.get("type") or "").strip().lower() == "password" for field in tree.iter("input")
    )


def _is_link_dense(root: HtmlElement) -> bool:
    links = [link for link in root.iter("a") if link.get("href")]
    if len(links) < LISTING_MIN_LINKS:
        return False
    total = len(" ".join(root.text_content().split()))
    if total == 0:
        return False
    in_links = sum(len(" ".join(link.text_content().split())) for link in links)
    return in_links / total >= LISTING_LINK_SHARE


# The signals in the order of ``PageSignal``, and those that decide, strongest first.
_SIGNAL_ORDER: Final[tuple[PageSignal, ...]] = (
    "status",
    "soft_404",
    "root_path",
    "schema_org",
    "og_type",
    "url",
    "title",
    "h1",
    "password_field",
    "pagination",
    "link_density",
    "word_count",
)
_DECISIVE: Final[tuple[PageSignal, ...]] = tuple(s for s in _SIGNAL_ORDER if s != "og_type")


def page_type(
    html: str, url: str, *, status: int | None, quality: "Quality", min_words: int
) -> PageTypeInfo:
    """Classify a fetched page; see the module docstring for the precedence."""
    # Imported here: at module level it would close a cycle (convert.links -> page_types ->
    # quality -> convert.markup -> the convert package).
    from text_extraction.quality import THIN_PAGE_BELOW_WORDS

    error_signals: list[PageSignal] = []
    if status is not None and status >= 400:
        error_signals.append("status")
    if quality.soft_404:
        error_signals.append("soft_404")
    if error_signals:
        return PageTypeInfo(type="error", signals=error_signals)

    by_url = url_type(url)
    if by_url == "home":
        return PageTypeInfo(type="home", signals=["root_path"])

    tree = parse_page(html)
    votes: dict[PageSignal, PageType] = {}
    if (schema := _schema_page_type(tree)) is not None:
        votes["schema_org"] = schema
    if _has_og_article(tree):
        votes["og_type"] = "content"
    if by_url is not None:
        votes["url"] = by_url

    title = tree.find(".//title")
    if (by_title := _heading_type(title.text_content() if title is not None else None)) is not None:
        votes["title"] = by_title

    for element in list(tree.iter(*_NON_VISIBLE)):
        element.drop_tree()
    heading = tree.find(".//h1")
    if (
        by_h1 := _heading_type(heading.text_content() if heading is not None else None)
    ) is not None:
        votes["h1"] = by_h1

    if quality.word_count < THIN_PAGE_BELOW_WORDS and _has_password_input(tree):
        votes["password_field"] = "auth"  # noqa: S105 - a signal name, not a secret
    if _has_pagination(tree):
        votes["pagination"] = "listing"
    body = tree.find(".//body")
    if _is_link_dense(body if body is not None else tree):
        votes["link_density"] = "listing"
    if quality.word_count >= min_words:
        votes["word_count"] = "content"

    winner: PageType = next((votes[s] for s in _DECISIVE if s in votes), "other")
    if winner == "other":
        return PageTypeInfo(type="other", signals=[])
    return PageTypeInfo(type=winner, signals=[s for s in _SIGNAL_ORDER if votes.get(s) == winner])
