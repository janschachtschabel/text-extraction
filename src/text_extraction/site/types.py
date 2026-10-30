"""Models of ``POST /v1/site-structure`` (rulings P9-S1, P9-S2): the request, the options a
``site`` job stores, the crawl's nodes and scope, and the ``site`` part of an item's result.
No parser and no Valkey here: the request models, the job models and ``ExtractResult`` import
it."""

import math
from typing import Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from text_extraction.compliance import ComplianceMode
from text_extraction.netguard import MAX_URL_LENGTH

__all__ = [
    "LASTMOD_MAX_CHARS",
    "LINK_TEXT_MAX_CHARS",
    "MAX_SITE_DEPTH",
    "MAX_SITE_PAGES",
    "Crawl",
    "LinkKind",
    "LinkRegion",
    "LinkSource",
    "SiteInfo",
    "SiteLink",
    "SiteNode",
    "SiteOptions",
    "SiteRole",
    "SiteScope",
    "SiteStructureRequest",
    "SitemapChild",
    "SitemapEntry",
    "SitemapFormat",
    "SitemapInfo",
]

# The widest bounds of a request; the route checks ``SITE_MAX_DEPTH`` and ``SITE_MAX_PAGES``,
# which may be lower, since a model cannot see the settings.
MAX_SITE_DEPTH = 10
MAX_SITE_PAGES = 100_000
LINK_TEXT_MAX_CHARS = 200  # of one link's text, whitespace collapsed
LASTMOD_MAX_CHARS = 64  # a longer ``lastmod`` is no date: it is dropped

SiteRole = Literal["page", "robots", "sitemap", "sitemap_part"]
LinkKind = Literal["internal", "external", "file"]
LinkSource = Literal["a", "area", "link", "iframe"]
LinkRegion = Literal["nav", "header", "footer", "aside", "main", "breadcrumb", "pagination"]
# What the crawl did with a link (P9-S6): ``listed`` (a head ``link`` or an ``iframe``: never
# followed), ``file``, ``out_of_scope``, ``nofollow``, ``blocked`` (robots.txt or TDM under
# ``enforce``), ``depth`` (deeper than asked), ``queued`` (a new item), ``seen`` (an existing
# item), ``max_pages`` (the page cap was reached), ``memory_guard`` (Valkey was nearly full).
Crawl = Literal[
    "listed",
    "file",
    "out_of_scope",
    "nofollow",
    "blocked",
    "depth",
    "queued",
    "seen",
    "max_pages",
    "memory_guard",
]
SitemapFormat = Literal["urlset", "index", "text", "rss", "atom", "absent"]
ChangeFreq = Literal["always", "hourly", "daily", "weekly", "monthly", "yearly", "never"]
_CHANGEFREQS: frozenset[str] = frozenset(get_args(ChangeFreq))


def _bounded_lastmod(value: object) -> object:
    """A sitemap's ``lastmod`` as given, stripped; blank or over 64 characters is None."""
    if isinstance(value, str):
        value = value.strip()
        return value if 0 < len(value) <= LASTMOD_MAX_CHARS else None
    return value


class SiteOptions(BaseModel):
    """The crawl options of a ``site`` job, stored as ``JobOptions.site``: the request without
    its URL and its compliance mode (the job's own ``compliance`` holds the resolved mode)."""

    model_config = ConfigDict(extra="forbid")

    depth: int = Field(
        default=2,
        ge=0,
        le=MAX_SITE_DEPTH,
        description="Link distance from the start page to follow, at most `SITE_MAX_DEPTH` "
        "(422 over it). `0` fetches the start page only (and the sitemaps).",
    )
    max_pages: int = Field(
        default=100,
        ge=1,
        le=MAX_SITE_PAGES,
        description="Most page items, the start page included, at most `SITE_MAX_PAGES` (422 "
        "over it). Links found after the cap say `crawl: max_pages`, and the page that found "
        "them has the warning `max_pages_reached`.",
    )
    sitemaps: bool = Field(
        default=True,
        description="Read robots.txt and every sitemap it lists (or `/sitemap.xml` when it lists "
        "none), with sitemap indexes, gzip, text, RSS and Atom sitemaps.",
    )
    scope: Literal["host", "path"] = Field(
        default="host",
        description="Which links are followed. `host`: the start page's host (a leading `www.` "
        "ignored). `path`: also only URLs under the start URL's directory.",
    )
    metadata: bool = Field(default=True, description="Include each page's DOM metadata.")
    cache: bool = Field(
        default=True,
        description="Use the result cache for pages; `false` neither reads nor stores.",
    )


class SiteStructureRequest(SiteOptions):
    """Body of ``POST /v1/site-structure``."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL of the start page.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's. "
        "Under `enforce` a page or sitemap that robots.txt disallows, or whose TDM rights are "
        "reserved, is not fetched (its link says `blocked`).",
    )

    def options(self) -> SiteOptions:
        """The options the job stores."""
        return SiteOptions.model_validate(self.model_dump(include=set(SiteOptions.model_fields)))


class SiteNode(BaseModel):
    """What the crawl knows of one item of a ``site`` job (stored per item, P9-S3)."""

    role: SiteRole
    depth: int  # page: link distance from the start; sitemap: nesting (robots-listed = 0)
    parent: int | None  # the item that found it; None for the start page
    guessed: bool = False  # a sitemap at /sitemap.xml that robots.txt did not list
    part: int | None = None  # sitemap_part: which part (2 and up) of its sitemap


class SiteScope(BaseModel):
    """Which URLs a crawl follows (D6 a): ``host`` without a leading ``www.``, and with
    ``scope: path`` only paths that start with ``path_prefix``."""

    host: str
    path_prefix: str | None


class SiteLink(BaseModel):
    """One outgoing link of a page (D2 a)."""

    url: str
    text: str = Field(description="The link's text, whitespace collapsed, at most 200 characters.")
    kind: LinkKind = Field(
        description="`internal`, `external` or `file`, as `links` of an extract."
    )
    source: LinkSource = Field(
        description="The element: `a`, `area`, `link` (in the head: `canonical`, `alternate`, "
        "`prev`, `next`) or `iframe`."
    )
    rel: list[str] = Field(description="The link's `rel` tokens, lower case, at most 8.")
    hreflang: str | None = None
    region: LinkRegion | None = Field(
        default=None,
        description="Where on the page: `nav`, `header`, `footer`, `aside`, `main`, "
        "`breadcrumb` or `pagination`; null elsewhere.",
    )
    crawl: Crawl = Field(
        description="What the crawl did with it: `listed`, `file`, `out_of_scope`, `nofollow`, "
        "`blocked`, `depth`, `queued`, `seen`, `max_pages` or `memory_guard`."
    )
    target: int | None = Field(
        default=None, description="The index of the item of the linked page, when it has one."
    )


class SitemapEntry(BaseModel):
    """One URL a sitemap lists. Its JSON leaves out null fields, so large sitemaps stay small;
    a value outside the protocol (a priority outside 0 to 1, an unknown ``changefreq``, a
    ``lastmod`` over 64 characters) is null."""

    loc: str
    lastmod: str | None = None  # as given
    changefreq: ChangeFreq | None = None
    priority: float | None = None  # 0 to 1

    @field_validator("lastmod", mode="before")
    @classmethod
    def _bounded_lastmod(cls, value: object) -> object:
        return _bounded_lastmod(value)

    @field_validator("changefreq", mode="before")
    @classmethod
    def _known_changefreq(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return value if value in _CHANGEFREQS else None
        return value

    @field_validator("priority", mode="before")
    @classmethod
    def _priority_in_range(cls, value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            return None
        try:
            number = float(value)  # float() takes the blanks around a number
        except (ValueError, OverflowError):  # OverflowError: an int too large for a float
            return None
        return number if math.isfinite(number) and 0 <= number <= 1 else None

    # No return annotation on purpose: with one, pydantic takes it as the serialized schema and
    # the OpenAPI spec would lose the entry's fields; without one it keeps the model's.
    @model_serializer(mode="wrap")
    def _drop_nulls(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data = handler(self)
        if isinstance(data, dict):
            return {name: value for name, value in data.items() if value is not None}
        return data


class SitemapChild(BaseModel):
    """One sitemap that a sitemap index lists."""

    url: str
    lastmod: str | None = None  # as given, at most 64 characters (as ``SitemapEntry.lastmod``)
    crawl: Crawl
    target: int | None = None  # the item of the child sitemap, when it has one

    @field_validator("lastmod", mode="before")
    @classmethod
    def _bounded_lastmod(cls, value: object) -> object:
        return _bounded_lastmod(value)


class SitemapInfo(BaseModel):
    """What one sitemap item (or part) holds."""

    format: SitemapFormat = Field(
        description="`urlset`, `index`, `text`, `rss`, `atom`, or `absent` (a guessed "
        "`/sitemap.xml` that does not exist)."
    )
    guessed: bool = Field(
        default=False, description="`/sitemap.xml` was tried because robots.txt listed none."
    )
    entries: list[SitemapEntry] = Field(
        default=[], description="The URLs of this part of the sitemap, in the file's order."
    )
    children: list[SitemapChild] = Field(
        default=[], description="An index's sitemaps (first part only)."
    )
    entry_count: int = Field(default=0, description="Valid URLs in the whole file.")
    part: int = Field(default=1, description="Which part this is, from 1.")
    parts: int = Field(default=1, description="How many parts the sitemap's entries fill.")


class SiteInfo(BaseModel):
    """The ``site`` part of a ``site`` job item's result."""

    role: SiteRole = Field(
        description="`page`, `robots` (the site's robots.txt), `sitemap` or `sitemap_part` (more "
        "entries of a large sitemap)."
    )
    depth: int = Field(
        description="Page: link distance from the start page. Sitemap: nesting below robots.txt "
        "(a sitemap robots.txt lists is 0)."
    )
    parent: int | None = Field(description="The index of the item that found it.")
    links: list[SiteLink] | None = Field(
        default=None, description="A page's outgoing links in document order; null if not HTML."
    )
    nofollow: bool = Field(
        default=False, description="The page said `nofollow` (meta robots or X-Robots-Tag)."
    )
    sitemaps: list[str] | None = Field(
        default=None, description="robots.txt: the sitemap URLs it listed, or the one guessed."
    )
    sitemap: SitemapInfo | None = None
