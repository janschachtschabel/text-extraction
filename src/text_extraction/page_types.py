"""Page types and link categories by rules (package 14): no I/O, no model, no network.

``url_type`` reads what a URL's path and query say about the page (an imprint, a login, a search
result, a listing, the home page). ``link_category`` puts a link of a page into a category for its
reader: ``url_type`` for a link inside the site, the host for a social network, the file kind for
a download, the link text for a navigation helper. ``page_type`` classifies a fetched page from
its signals, strongest first (ruling P14-S4):

1. ``error``: the status is 400 or higher, or the quality gate saw a soft 404.
2. ``home``: the path is empty, ``/`` or an index file, without a query besides tracking and
   language parameters (A26-R1). A path that is only a language code (``/it/``) is home only
   when the page's ``<html lang>`` is missing or names that language; otherwise the URL gives no
   vote (A26-R3).
3. schema.org: the JSON-LD ``@type`` (top-level nodes and the ``@graph``) or the ``itemtype`` of
   the outermost microdata scope says ``ContactPage``, ``AboutPage``, ``SearchResultsPage`` or
   ``CollectionPage``.
4. ``url_type`` (imprint, privacy, legal, auth, search, contact, about, listing).
5. The ``<title>`` starts with the name of a type (``Impressum``, ``Kontakt``, ``Login`` ...),
   followed by nothing (a trailing colon aside) or a separator other than a colon, or ends with
   one after a separator;
   the first ``<h1>`` is the name of a type and nothing else (A26-R4).
6. ``auth``: a password input on a thin page.
7. ``listing``: ``rel="next|prev"``, or a page that is mostly links.
8. ``content`` when the extracted content has enough words, else ``other``.

An article (``og:type`` is ``article``) with enough words drops the votes of the title, the
``<h1>``, the pagination, the link density and a ``url`` vote for ``listing`` before the winner
is chosen (A26-R5); a ``url`` vote for another type and schema.org stay.

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
import unicodedata
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from lxml.html import HtmlElement
from pydantic import BaseModel, Field

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
    folded = (
        unicodedata.normalize("NFC", text)
        .casefold()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
    )
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
    "contact": (
        "kontakt",
        "contact",
        "contact-us",
        "kontaktformular",
        "ansprechpartner",
        "ansprechpersonen",
        "kontakt-aufnehmen",
        "get-in-touch",
    ),
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
        "author",
        "authors",
        "autor",
        "autoren",
    ),
}
_SEGMENT_TYPE: Final[dict[str, PageType]] = {
    _fold(name): page_type_name
    for page_type_name, names in _SEGMENT_NAMES.items()
    for name in names
}
_LISTING_NAMES: Final = frozenset(_fold(name) for name in _SEGMENT_NAMES["listing"])
# Names that also name ordinary sections or articles (``/rezepte/cookies``, ``/glossary/terms``):
# they count only as the whole path, after an optional language code (A26-R2).
_AMBIGUOUS_NAMES: Final = frozenset(
    {
        *("cookies", "terms", "legal", "konto", "account", "profil", "profile", "recherche"),
        *("team", "password", "passwort", "disclaimer"),
    }
)
# Query parameters that say nothing about the page; home allows only these (A26-R1).
_TRACKING_PARAMETERS: Final = frozenset(
    {
        *("fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid"),
        *("mc_cid", "mc_eid", "_ga", "_gl", "ref"),
    }
)
_TRACKING_PREFIX: Final = "utm_"
# Query parameters that pick the language of a page; on a home path they keep it home (A26-R1):
# ``/?lang=en`` (WPML), ``/?hl=de`` (Google-style), ``/?locale=de_DE``.
_LANGUAGE_PARAMETERS: Final = frozenset({"lang", "hl", "locale"})
_SEARCH_PARAMETERS: Final = frozenset({"q", "query", "search", "suche", "s", "keyword"})
_PAGE_PARAMETERS: Final = frozenset({"page", "seite"})
_PAGE_SEGMENTS: Final = frozenset({"page", "seite"})
_FILE_EXTENSION = re.compile(r"\.(?:html?|php|aspx?)$")
_INDEX_FILE = re.compile(r"^index\.(?:html?|php)$", re.IGNORECASE)
# A language code as a path segment, with an optional region (``de``, ``de-at``, ``pt_BR``).
_LANGUAGE_SEGMENT = re.compile(r"^(?P<language>[a-z]{2})(?:[-_][a-z]{2})?$", re.IGNORECASE)
# Empty, ``/``, ``/index.html``, and a language code in front (``/de``, ``/en/``, ``/de-at/``).
_HOME_PATH = re.compile(
    r"^/?(?:(?P<language>[a-z]{2})(?:[-_][a-z]{2})?(?:/|$))?(?:index\.(?:html?|php))?$",
    re.IGNORECASE,
)
# The languages a site realistically puts in front of its paths: the 24 official languages of
# the EU, the other languages of Europe and its neighbours, and the large languages elsewhere.
# Deliberately not all 184 ISO 639-1 codes: some of them are common section names (``/ki/`` is
# Kikuyu, but on a German site it is the AI section), and two letters that are no language at
# all (``/ai/``, ``/ui/``) are no start page either. The region after the language (``de-at``,
# ``en_GB``) is not checked.
_SITE_LANGUAGES: Final = frozenset(
    {
        # EU official languages
        *("bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr", "hu"),
        *("it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv"),
        # other European and neighbouring languages
        *("ca", "is", "lb", "no", "nb", "nn", "sq", "sr", "bs", "mk", "ru", "uk", "tr"),
        # large languages elsewhere
        *("ar", "he", "fa", "hi", "zh", "ja", "ko", "vi", "th", "id", "ms"),
    }
)


def _segment_stem(segment: str) -> str:
    return _FILE_EXTENSION.sub("", _fold(segment)).replace("_", "-")


def _site_language(segment: str) -> str | None:
    """The language code (without its region) when ``segment`` is a site language, else None."""
    match = _LANGUAGE_SEGMENT.match(segment)
    if match is None or match["language"].lower() not in _SITE_LANGUAGES:
        return None
    return match["language"].lower()


def _segment_types(stems: list[str]) -> set[PageType]:
    """The types the path segments say by their position (A26-R2): a listing name with at most
    one segment after it, an ambiguous name as the only segment, any other name as the first or
    the last segment; a leading language code does not count as a segment."""
    if stems and _site_language(stems[0]) is not None:
        stems = stems[1:]
    found: set[PageType] = set()
    last = len(stems) - 1
    for index, stem in enumerate(stems):
        if stem not in _SEGMENT_TYPE:
            continue
        if stem in _LISTING_NAMES:
            counts = last - index <= 1
        elif stem in _AMBIGUOUS_NAMES:
            counts = last == 0
        else:
            counts = index in (0, last)
        if counts:
            found.add(_SEGMENT_TYPE[stem])
    return found


def _is_tracking(name: str) -> bool:
    return name.startswith(_TRACKING_PREFIX) or name in _TRACKING_PARAMETERS


def url_type(url: str) -> PageType | None:
    """The type that the path and the query of ``url`` say, else None.

    A path segment (or its stem before ``.html``, ``.htm``, ``.php``) has to be a name in full:
    ``/impressum.html`` is an imprint, ``/impressum-archiv-2019`` is not. A trailing index file
    is dropped first. A listing name counts with at most one segment after it, a few ambiguous
    names (``cookies``, ``terms``, ``team`` ...) only as the whole path, any other name as the
    first or the last segment; a leading language code is skipped (A26-R2). Home is an empty
    path, ``/``, an index file or only a language code (optionally with an index file), and
    every query parameter is a tracking parameter, a language parameter (``lang``, ``hl``,
    ``locale``) or empty (A26-R1).
    """
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query)
    except ValueError:
        return None
    path = unicodedata.normalize("NFC", unquote(parts.path))
    segments = [segment for segment in path.split("/") if segment]
    if segments and _INDEX_FILE.match(segments[-1]):
        segments.pop()
    stems = [_segment_stem(segment) for segment in segments]

    found = _segment_types(stems)
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
    if any(
        value.strip() and not _is_tracking(name) and name not in _LANGUAGE_PARAMETERS
        for name, value in ((key.lower(), value) for key, value in query)
    ):
        return None
    home = _HOME_PATH.match(path)
    if home is not None and (home["language"] or "en").lower() in _SITE_LANGUAGES:
        return "home"
    return None


_DATED_PATH: Final = re.compile(
    r"/(?:19|20)\d\d/(?:0[1-9]|1[0-2])/(?:(?:0[1-9]|[12]\d|3[01])/)?(?!\d+/?$)[^/]+"
)
_WORDPRESS_QUERIES: Final = frozenset({"p", "page_id"})


def guess_url_type(url: str) -> PageType | None:
    """The type of a URL that was not fetched: ``url_type``, else ``content`` for a dated path
    (``/2021/05/oer-camp/``) or a WordPress ``?p=12`` / ``?page_id=12`` query, else None.
    ``url_type`` itself never says ``content``, because ``page_type`` uses it as a signal."""
    known = url_type(url)
    if known is not None:
        return known
    try:
        parts = urlsplit(url)
        query = [(k.lower(), v) for k, v in parse_qsl(parts.query) if not _is_tracking(k.lower())]
    except ValueError:
        return None
    if len(query) == 1 and query[0][0] in _WORDPRESS_QUERIES and query[0][1].isdecimal():
        return "content"
    if not query and _DATED_PATH.search(unicodedata.normalize("NFC", unquote(parts.path))):
        return "content"
    return None


def _path_language(url: str) -> str | None:
    """The language code (without its region) of a path that is only a language code, optionally
    with an index file (``/it/``, ``/de-at/index.html``), else None."""
    try:
        path = unicodedata.normalize("NFC", unquote(urlsplit(url).path))
    except ValueError:
        return None
    home = _HOME_PATH.match(path)
    return home["language"].lower() if home is not None and home["language"] else None


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


def is_social_url(url: str) -> bool:
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
        return "social" if is_social_url(url) else "content"
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
_HEADING_WORDS_PATTERN = "|".join(
    re.escape(word) for word in sorted(_HEADING_TYPE, key=len, reverse=True)
)
# The name first, then nothing or a separator (a hyphen only with a space before or after it).
# A colon is no separator here: after a type word it starts a subtitle ("Datenschutz: Was
# Schulen wissen müssen"; A26-R4). A colon with nothing after it is no subtitle ("Impressum:").
_HEADING_START = re.compile(
    rf"^(?P<word>{_HEADING_WORDS_PATTERN})(?=\s*:?\s*$|\s*[|\u2013\u2014\u00b7]|\s+-|-\s)"
)
# Or the name last, after a separator (a hyphen only with a space before or after it).
_HEADING_END = re.compile(
    rf"(?:[|\u2013\u2014:\u00b7]|\s-|-\s)\s*(?P<word>{_HEADING_WORDS_PATTERN})$"
)

_SCHEMA_TYPES: Final[tuple[tuple[str, PageType], ...]] = (
    ("ContactPage", "contact"),
    ("AboutPage", "about"),
    ("SearchResultsPage", "search"),
    ("CollectionPage", "listing"),
)
_NON_VISIBLE: Final = ("script", "style", "noscript", "template")
# Primary subtags of a ``lang`` that names no language: undetermined, multiple, no linguistic
# content, and the private-use prefix (BCP 47).
_NO_LANGUAGE_SUBTAGS: Final = frozenset({"und", "mul", "zxx", "x"})


class PageTypeInfo(BaseModel):
    """The type of a page and the signals that agree with it."""

    type: PageType = Field(
        description="What kind of page this is, by rules: `home`, `content` (an article or "
        "another page with enough text), `listing` (an index of other pages: a category, a tag, "
        "an archive, a page of a paged list), `imprint`, `privacy`, `legal` (terms, cookies, "
        "disclaimer, accessibility statement), `contact`, `about`, `auth` (login, sign-up, "
        "account), `search` (search results), `error` (an error page, such as a soft 404) or "
        "`other`."
    )
    signals: list[PageSignal] = Field(
        description="Every signal that agrees with `type`, strongest first: `status`, "
        "`soft_404`, `root_path`, `schema_org` (JSON-LD or microdata), `og_type` (`og:type` is "
        "`article`; never decides), `url` (the path or the query), `title`, `h1`, "
        "`password_field`, `pagination` (`rel=next|prev`), `link_density` (mostly links), "
        "`word_count`. Empty for `other`."
    )


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


def _microdata_scopes(tree: HtmlElement) -> list[HtmlElement]:
    """The outermost ``itemscope`` elements in document order, at most ``_MAX_MICRODATA_SCOPES``;
    a walk that does not descend into a scope, so deep nesting costs nothing extra."""
    scopes: list[HtmlElement] = []
    stack = [tree]
    while stack and len(scopes) < _MAX_MICRODATA_SCOPES:
        element = stack.pop()
        if not isinstance(element.tag, str):  # a comment or a processing instruction
            continue
        if element.get("itemscope") is not None:
            scopes.append(element)
            continue
        stack.extend(reversed(element))
    return scopes


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
    for scope in _microdata_scopes(tree):
        names.update(_schema_name(item) for item in (scope.get("itemtype") or "").split())
    return names


def _schema_page_type(tree: HtmlElement) -> PageType | None:
    names = _schema_type_names(tree)
    for name, found in _SCHEMA_TYPES:
        if name in names:
            return found
    return None


def _title_type(text: str | None) -> PageType | None:
    folded = _fold((text or "")[:_MAX_HEADING_CHARS])
    match = _HEADING_START.match(folded) or _HEADING_END.search(folded)
    return _HEADING_TYPE[match.group("word")] if match else None


def _h1_type(text: str | None) -> PageType | None:
    """The type when the whole ``<h1>`` is a type word, a trailing colon aside (A26-R4)."""
    folded = _fold((text or "")[:_MAX_HEADING_CHARS]).removesuffix(":").rstrip()
    return _HEADING_TYPE.get(folded)


def _html_language(tree: HtmlElement) -> str | None:
    """The primary subtag of the root element's ``lang`` (``de`` of ``de-AT``), else None; a
    ``lang`` that names no language (``und``, ``mul``, ``zxx``, a private ``x-...``) is None."""
    lang = (tree.getroottree().getroot().get("lang") or "").strip()
    primary = re.split(r"[-_]", lang, maxsplit=1)[0].lower()
    return primary if primary and primary not in _NO_LANGUAGE_SUBTAGS else None


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
# The votes an article with enough words drops (A26-R5); a ``url`` vote only for ``listing``.
_ARTICLE_DROPS: Final[tuple[PageSignal, ...]] = ("title", "h1", "pagination", "link_density")


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
    language = _path_language(url) if by_url == "home" else None
    if by_url == "home" and language is None:
        return PageTypeInfo(type="home", signals=["root_path"])

    tree = parse_page(html)
    if by_url == "home":
        # A path that is only a language code (A26-R3): home unless the page says another one.
        if _html_language(tree) in (None, language):
            return PageTypeInfo(type="home", signals=["root_path"])
        by_url = None
    votes: dict[PageSignal, PageType] = {}
    if (schema := _schema_page_type(tree)) is not None:
        votes["schema_org"] = schema
    is_article = _has_og_article(tree)
    if is_article:
        votes["og_type"] = "content"
    if by_url is not None:
        votes["url"] = by_url

    title = tree.find(".//title")
    if (by_title := _title_type(title.text_content() if title is not None else None)) is not None:
        votes["title"] = by_title

    for element in list(tree.iter(*_NON_VISIBLE)):
        element.drop_tree()
    heading = tree.find(".//h1")
    if (by_h1 := _h1_type(heading.text_content() if heading is not None else None)) is not None:
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
        if is_article:
            for signal in _ARTICLE_DROPS:
                votes.pop(signal, None)
            if votes.get("url") == "listing":
                del votes["url"]

    winner: PageType = next((votes[s] for s in _DECISIVE if s in votes), "other")
    if winner == "other":
        return PageTypeInfo(type="other", signals=[])
    return PageTypeInfo(type=winner, signals=[s for s in _SIGNAL_ORDER if votes.get(s) == winner])
