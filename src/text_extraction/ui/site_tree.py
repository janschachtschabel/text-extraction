"""The site tree of a ``site`` job (ruling P10-S20, Jan's requirement 2). Pure: no I/O.

The hierarchy is the URL's path segments: to editors, site sections are URL directories, and
sitemap entries, which have no parent page, slot into a path tree (the link graph's ``parent``
is the first page that happened to find a URL, so a parent tree would move between runs).

Three sources are merged by ``seen_key`` (package 9), in this order of precedence:

1. page items (``site.role == "page"``): URL, title, status and link depth;
2. the entries of ``sitemap`` and ``sitemap_part`` items (``loc``, ``lastmod``);
3. links of crawled pages that were not followed for ``depth`` or ``max_pages`` (linked only).

Each source keeps at most ``max_urls`` URLs (``UI_TREE_MAX_URLS``), and the tree shows at most
``max_urls``: the pages first, then entries, then links. The rest is counted as ``hidden``. The
items are taken one at a time (`TreeBuilder.add`), so a reader can feed them page by page.

Each URL leaf has a page type (package 14): a crawled page its result's ``page_type`` (none
when the page failed or its item was stored before page types, ruling P14-R7); a sitemap entry
or a linked-only URL the guess of ``page_types.guess_type`` (ruling P18-R1: from its URL, the
sitemap's ``type_hint``, a link's text and region, or ``content`` by default), always
``type_guessed`` with its ``type_reason``; only a URL with no name and an id shape stays without
one (a link's ``category`` is not used). A URL known from several sources takes the guess of the
strongest, unless that guess is ``content`` by default and a weaker source gives a reason other
than a link text for a sitemap entry (AU3-R2: a link "Datenschutz" leaves a listed page
content). The
first link that names a URL sets its title (``title_from_link``); a crawled page's own title
wins. A section has ``pages`` (the URL leaves below it), ``section_type`` (the
type all of them share, guessed or fetched, else none) and ``section_guessed`` (a leaf below is
guessed). ``SiteTree.type_counts`` counts the types of the URLs shown that have one,
``SiteTree.untyped`` the shown URLs that were not fetched and have none, ``found_counts`` the
shown URLs of each kind. ``SiteTree.social`` lists the external social links of the crawled
pages (``category`` ``social``, or for an item stored without categories the host), each once by
host and path, at most ``TREE_MAX_SOCIAL``; share and intent endpoints (``facebook.com/sharer.php``,
``x.com/intent/post``) name no profile and are left out.

A leaf can be marked as blocked (``TreeNode.blocked``, a ``BlockReason``): a crawled page by
its own compliance report (`TreeBuilder.add`), then every other leaf by the view's judgement of
its URL against its origin's rules under ``enforce`` (`mark_blocked`, which does no I/O: the
caller loads the rules per origin, `leaf_origins`). Origins tried whose rules could not be
read or judged in time (the caller's budgets, `mark_blocked`'s deadline) are named
(``SiteTree.unchecked``); origins not tried (past the caller's cap) are counted
(``SiteTree.unchecked_more``). Their leaves stay unmarked.

Roots are hosts (a leading ``www.`` dropped, as in ``seen_key``); a section is a directory
(its label ends in ``/``) and has no URL of its own; every URL is a leaf: a directory's own
page is the leaf ``/`` inside it, and a query string makes a leaf of its own. A section whose
only child is that leaf is one row: the leaf, with the section's label. Children are in name
order."""

import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Final, Literal, get_args
from urllib.parse import urlsplit

from text_extraction.compliance import (
    BlockReason,
    ComplianceMode,
    OriginRules,
    block_reason,
    origin_of,
)
from text_extraction.job_models import JobItem
from text_extraction.page_types import GuessReason, PageType, guess_type, is_social_url
from text_extraction.site.types import SiteInfo, SiteLink
from text_extraction.site.urls import seen_key
from text_extraction.ui.i18n import Message

__all__ = [
    "FOUND_LABELS",
    "MAX_SECTION_DEPTH",
    "NON_CONTENT_TYPES",
    "TREE_MAX_SOCIAL",
    "NodeKind",
    "SiteTree",
    "SocialLink",
    "TreeBuilder",
    "TreeNode",
    "build_tree",
    "leaf_origins",
    "mark_blocked",
]

NodeKind = Literal["section", "page", "sitemap", "linked"]
# How the tree labels a URL leaf of each kind (a section has no URL and no label).
FOUND_LABELS: Final[Mapping[NodeKind, Message]] = MappingProxyType(
    {
        "page": Message("tree.crawled"),
        "sitemap": Message("tree.in_sitemap"),
        "linked": Message("tree.linked_only"),
    }
)

# Sections below a host; the segments of a deeper path stay together in its leaf's label, so
# neither the tree nor its rendering nests without bound.
MAX_SECTION_DEPTH: Final = 12
# The social links listed for the site, at most.
TREE_MAX_SOCIAL: Final = 50
# A network's share and intent endpoints (a "share this page" button), which name no profile:
# the first path segment, or a path and what follows it, lower-cased.
_SHARE_SEGMENTS: Final = frozenset(
    {"intent", "share", "share.php", "sharearticle", "sharer", "sharer.php", "sharing", "submit"}
)
_SHARE_PATHS: Final = ("/dialog/feed", "/dialog/share", "/pin/create", "/send", "/spi/shares")
# Hidden URLs are counted once each up to this many (by their key's hash); beyond it, each
# further mention counts, so the count never needs more memory than this.
HIDDEN_KEYS_MAX: Final = 250_000
# Links not followed for these reasons are shown as linked only; a followed link has an item.
# A "blocked" link carries no reason of its own: `mark_blocked` judges it.
LINKED_ONLY: Final = frozenset({"depth", "max_pages", "blocked", "sitemap_first"})
# The page types that "Select content pages" leaves unticked; a leaf of any other type, or
# without one, is ticked.
NON_CONTENT_TYPES: Final[tuple[PageType, ...]] = (
    "imprint",
    "privacy",
    "legal",
    "contact",
    "auth",
    "search",
    "error",
)
_PAGE_TYPES: Final[tuple[PageType, ...]] = get_args(PageType)
_PAGE, _ENTRY, _LINKED = 0, 1, 2
_KINDS: Final[tuple[NodeKind, ...]] = ("page", "sitemap", "linked")


@dataclass
class TreeNode:
    """One node: a section (no URL) or a URL leaf of ``kind`` ``page`` (crawled), ``sitemap``
    (listed only) or ``linked`` (linked only). ``status`` is a page's result status and
    ``error_code`` its error's code, ``depth`` its link distance from the start page,
    ``lastmod`` a sitemap's, ``page_type`` its type (``type_guessed`` when from its URL),
    ``type_reason`` what a guessed type rests on, ``title_from_link`` that the title is a link's
    text, ``blocked`` why the site's rules block it (none: no rule does, or it is not known).
    A section has ``pages`` (URL leaves below it), ``section_type`` (the type they all share,
    none if one has none or they differ) and ``section_guessed`` (any of them is guessed)."""

    label: str
    url: str | None
    kind: NodeKind
    title: str | None = None
    status: str | None = None
    depth: int | None = None
    lastmod: str | None = None
    children: list["TreeNode"] = field(default_factory=list)
    error_code: str | None = None
    page_type: PageType | None = None
    type_guessed: bool = False
    type_reason: GuessReason | None = None
    title_from_link: bool = False
    blocked: BlockReason | None = None
    pages: int = 0
    section_type: PageType | None = None
    section_guessed: bool = False


@dataclass(frozen=True)
class SocialLink:
    """An external link to a social network: its URL and its host (without ``www.``)."""

    url: str
    host: str


@dataclass(frozen=True)
class SiteTree:
    """The roots (one per host), the URLs shown and how many more were not shown;
    ``type_counts`` the shown URLs of each page type, most first (then in the order of
    ``PageType``), without the URLs that have no type. ``crawl_mode`` is the compliance mode
    of the crawl (none when no item says), ``blocked`` the leaves with a block reason and
    ``unchecked`` the origins tried whose leaves could not be judged and ``unchecked_more`` the
    number of origins not tried (`mark_blocked`). ``untyped`` counts the shown URLs that were
    not fetched (sitemap or linked) and have no page type; ``social`` the social links;
    ``found_counts`` the shown URLs of each kind (``page``, ``sitemap``, ``linked``)."""

    roots: list[TreeNode]
    urls: int
    hidden: int
    type_counts: Mapping[PageType, int] = field(default_factory=dict)
    crawl_mode: ComplianceMode | None = None
    blocked: int = 0
    unchecked: tuple[str, ...] = ()
    unchecked_more: int = 0
    untyped: int = 0
    social: tuple[SocialLink, ...] = ()
    found_counts: Mapping[NodeKind, int] = field(default_factory=dict)


@dataclass
class _Found:
    """What the sources say of one URL."""

    url: str
    title: str | None = None
    status: str | None = None
    depth: int | None = None
    lastmod: str | None = None
    error_code: str | None = None
    page_type: PageType | None = None
    type_guessed: bool = False
    type_reason: GuessReason | None = None
    title_from_link: bool = False
    blocked: BlockReason | None = None
    from_sitemap: bool = False

    def merge(self, other: "_Found") -> None:
        if not self.title:
            self.title, self.title_from_link = other.title, other.title_from_link
        if self.status is None:  # not a crawled page (only a page has a status)
            # A crawled page's own verdict, even none, wins over a type from a URL; between
            # guesses, a weaker source's reason beats a stronger source's bare default, but a
            # link text never beats a sitemap entry's (AU3-R2).
            if (
                other.status is not None
                or self.page_type is None
                or (
                    self.type_reason == "default"
                    and other.type_reason not in (None, "default")
                    and not (self.from_sitemap and other.type_reason == "link_text")
                )
            ):
                self.page_type, self.type_guessed = other.page_type, other.type_guessed
                self.type_reason = other.type_reason
            self.status, self.error_code = other.status, other.error_code
        self.depth = self.depth if self.depth is not None else other.depth
        self.lastmod = self.lastmod or other.lastmod
        self.blocked = self.blocked or other.blocked


class TreeBuilder:
    """Collects the URLs of a site job's items (`add`), at most ``max_urls`` per source, and
    builds the tree (`tree`)."""

    def __init__(self, max_urls: int) -> None:
        self._max = max_urls
        self._sources: tuple[dict[str, _Found], ...] = ({}, {}, {})
        self._hidden: set[int] = set()
        self._hidden_more = 0
        self._mode: ComplianceMode | None = None
        self._social: dict[str, SocialLink] = {}

    def add(self, item: JobItem) -> None:
        """Take what ``item`` says: a page, a sitemap's entries or a page's linked-only URLs.
        Items without a result (still queued) and robots.txt add nothing."""
        envelope = item.result
        data = envelope.data if envelope is not None else None
        site: SiteInfo | None = data.site if data is not None else None
        if envelope is None or data is None:
            return
        if self._mode is None and data.compliance is not None:
            self._mode = data.compliance.mode
        if site is None:
            return
        if site.role == "page" and item.url:
            code = str(envelope.error.code) if envelope.error is not None else None
            found = _Found(
                item.url,
                title=data.title,
                status=str(envelope.status),
                depth=site.depth,
                error_code=code,
                page_type=data.page_type.type if data.page_type is not None else None,
                blocked=block_reason(data.compliance) if data.compliance is not None else None,
            )
            self._add(_PAGE, found)
            for link in site.links or []:
                if link.crawl in LINKED_ONLY:
                    self._add(_LINKED, _linked(link))
                if link.kind == "external":
                    self._add_social(link.url, link.category)
        elif site.role in ("sitemap", "sitemap_part") and site.sitemap is not None:
            for entry in site.sitemap.entries:
                found = _guessed(entry.loc, sitemap_hint=site.sitemap.type_hint)
                if entry.type_guess is not None:
                    # The sitemap item stored the guess (the same function), so the tree and the
                    # API agree; the reason still comes from the guess above. An item stored
                    # before the field has none and keeps the guess made here.
                    found.page_type, found.type_guessed = entry.type_guess, True
                    found.type_reason = found.type_reason or "default"
                found.lastmod, found.from_sitemap = entry.lastmod, True
                self._add(_ENTRY, found)

    def _add_social(self, url: str, category: str | None) -> None:
        if len(self._social) >= TREE_MAX_SOCIAL:
            return
        if category != "social" and not (category is None and is_social_url(url)):
            return
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        host = (parts.hostname or "").lower().removeprefix("www.")
        if host and not _is_share_url(host, parts.path, parts.query):
            self._social.setdefault(f"{host}{parts.path}", SocialLink(url, host))

    def _add(self, rank: int, found: _Found) -> None:
        key = seen_key(found.url)
        target = self._sources[rank]
        for held_rank, source in enumerate(self._sources):
            held = source.get(key)
            if held is None:
                continue
            if held_rank <= rank:  # known from this source or a stronger one
                held.merge(found)
            elif len(target) < self._max:  # known from a weaker source: it moves up
                del source[key]
                found.merge(held)
                target[key] = found
            else:
                held.merge(found)
            return
        if len(target) < self._max:
            target[key] = found
            self._hidden.discard(hash(key))
        else:
            self._hide(key)

    def _hide(self, key: str) -> None:
        digest = hash(key)
        if digest in self._hidden:
            return
        if len(self._hidden) < HIDDEN_KEYS_MAX:
            self._hidden.add(digest)
        else:
            self._hidden_more += 1

    def tree(self) -> SiteTree:
        """The tree of at most ``max_urls`` URLs: pages, then entries, then linked-only URLs."""
        shown: list[tuple[NodeKind, _Found]] = []
        hidden = len(self._hidden) + self._hidden_more
        for rank, source in enumerate(self._sources):
            room = self._max - len(shown)
            found = list(source.values())
            shown.extend((_KINDS[rank], one) for one in found[:room])
            hidden += max(0, len(found) - room)
        counts = Counter(one.page_type for _, one in shown if one.page_type is not None)
        ordered = sorted(counts.items(), key=lambda pair: (-pair[1], _PAGE_TYPES.index(pair[0])))
        return SiteTree(
            roots=_assemble(shown),
            urls=len(shown),
            hidden=hidden,
            type_counts=dict(ordered),
            crawl_mode=self._mode,
            blocked=sum(1 for _, one in shown if one.blocked is not None),
            untyped=sum(1 for kind, one in shown if kind != "page" and one.page_type is None),
            social=tuple(self._social.values()),
            found_counts={kind: sum(1 for k, _ in shown if k == kind) for kind in _KINDS},
        )


def _is_share_url(host: str, path: str, query: str) -> bool:
    """Whether a social link is a share or intent endpoint (``facebook.com/sharer.php``,
    ``x.com/intent/post``, ``wa.me/?text=``), not a profile of the site."""
    path = path.lower()
    if path.strip("/").split("/", 1)[0] in _SHARE_SEGMENTS:
        return True
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _SHARE_PATHS):
        return True
    return host == "wa.me" and path in ("", "/") and "text=" in query


def _guessed(
    url: str,
    *,
    link_text: str = "",
    region: str | None = None,
    sitemap_hint: PageType | None = None,
) -> _Found:
    """A URL that was not crawled, with its guessed type and the reason (none if nothing says)."""
    guess = guess_type(url, link_text=link_text, region=region, sitemap_hint=sitemap_hint)
    if guess is None:
        return _Found(url)
    return _Found(url, page_type=guess.type, type_guessed=True, type_reason=guess.reason)


def _linked(link: SiteLink) -> _Found:
    """A linked-only URL: guessed from its URL, text and region, titled by its text."""
    link_text = link.text or ""  # an item stored with no text
    found = _guessed(link.url, link_text=link_text, region=link.region)
    text = " ".join(link_text.split())
    if text:
        found.title, found.title_from_link = text, True
    return found


def build_tree(items: Iterable[JobItem], *, max_urls: int) -> SiteTree:
    """The site tree of ``items`` (any order), with at most ``max_urls`` URLs."""
    builder = TreeBuilder(max_urls)
    for item in items:
        builder.add(item)
    return builder.tree()


def _place(key: str) -> tuple[str, list[str], str]:
    """The host, the section labels and the leaf label of a URL's seen key."""
    host, _, rest = key.partition("/")
    path, mark, query = rest.partition("?")
    segments = path.split("/")
    sections, name = segments[:-1], segments[-1] or "/"
    if mark:
        name = f"{name}{mark}{query}"
    if len(sections) > MAX_SECTION_DEPTH:
        name = "/".join([*sections[MAX_SECTION_DEPTH:], name])
        sections = sections[:MAX_SECTION_DEPTH]
    return host, [f"{segment}/" for segment in sections], name


def _assemble(shown: list[tuple[NodeKind, _Found]]) -> list[TreeNode]:
    roots: dict[str, TreeNode] = {}
    sections: dict[tuple[str, ...], TreeNode] = {}
    for kind, found in shown:
        host, labels, name = _place(seen_key(found.url))
        node = roots.get(host)
        if node is None:
            node = roots[host] = TreeNode(label=host, url=None, kind="section")
        path: tuple[str, ...] = (host,)
        for label in labels:
            path = (*path, label)
            child = sections.get(path)
            if child is None:
                child = sections[path] = TreeNode(label=label, url=None, kind="section")
                node.children.append(child)
            node = child
        node.children.append(
            TreeNode(
                label=name,
                url=found.url,
                kind=kind,
                title=found.title,
                status=found.status,
                error_code=found.error_code,
                depth=found.depth,
                lastmod=found.lastmod,
                page_type=found.page_type,
                type_guessed=found.type_guessed,
                type_reason=found.type_reason,
                title_from_link=found.title_from_link,
                blocked=found.blocked,
            )
        )
    ordered = sorted(roots.values(), key=_order)
    stack = list(ordered)
    while stack:
        node = stack.pop()
        node.children.sort(key=_order)
        node.children = [_folded(child) for child in node.children]
        stack.extend(child for child in node.children if child.children)
    for root in ordered:
        _summarize(root)
    return ordered


def _summarize(node: TreeNode) -> tuple[int, set[PageType | None], bool]:
    """Set ``pages``, ``section_type`` and ``section_guessed`` of the sections below and at
    ``node``; return the URL leaves below it, their types (None for a leaf without one) and
    whether any is guessed."""
    if node.url is not None:
        return 1, {node.page_type}, node.type_guessed
    pages, types, guessed = 0, set[PageType | None](), False
    for child in node.children:
        count, kinds, was_guessed = _summarize(child)
        pages, guessed = pages + count, guessed or was_guessed
        types |= kinds
    node.pages, node.section_guessed = pages, guessed
    node.section_type = next(iter(types)) if len(types) == 1 else None
    return pages, types, guessed


def _folded(node: TreeNode) -> TreeNode:
    """``node``, or when it is a section whose only child is its own page (the leaf ``/``, with
    no children), that leaf with the section's label: one row for a lone page."""
    if node.url is None and len(node.children) == 1:
        [only] = node.children
        if only.label == "/" and not only.children:
            return replace(only, label=node.label)
    return node


def _order(node: TreeNode) -> tuple[str, str]:
    return node.label.casefold(), node.label


def _leaves(tree: SiteTree) -> Iterable[TreeNode]:
    """The URL leaves in the order the tree shows them."""
    stack = list(reversed(tree.roots))
    while stack:
        node = stack.pop()
        if node.url is not None:
            yield node
        stack.extend(reversed(node.children))


def leaf_origins(tree: SiteTree) -> dict[str, str]:
    """The origins of the tree's leaves, each with the first leaf URL of it, the origin with
    most leaves first (then by the origin's text). Leaves of no http(s) origin are left out."""
    first: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for node in _leaves(tree):
        origin = origin_of(node.url) if node.url is not None else None
        if origin is None or node.url is None:
            continue
        first.setdefault(origin, node.url)
        counts[origin] += 1
    return {origin: first[origin] for origin in sorted(first, key=lambda o: (-counts[o], o))}


def mark_blocked(
    tree: SiteTree, rules: Mapping[str, OriginRules | None], deadline: float | None = None
) -> SiteTree:
    """``tree`` with every leaf marked that its origin's rules block under ``enforce``. ``rules``
    maps an origin (as `leaf_origins` gives it) to its rules, ``None`` for an origin that was
    tried and could not be read (``SiteTree.unchecked``); an origin absent from it was not tried
    (``SiteTree.unchecked_more`` counts them). Where the origin's rules are known, the current
    judgement decides; the crawl's own reason stays only for ``tdm`` (a page's ``noai`` is known
    only after its fetch) and for unchecked origins. ``deadline`` (a ``time.monotonic`` value)
    bounds the work: origins with a leaf not judged by then are ``unchecked``. Pure: the nodes
    are updated in place."""
    blocked = 0
    cut: set[str] = set()
    for node in _leaves(tree):
        origin = origin_of(node.url) if node.url is not None else None
        known = rules.get(origin) if origin is not None else None
        if known is not None and node.url is not None and origin is not None:
            if deadline is not None and time.monotonic() >= deadline:
                cut.add(origin)
            else:
                judged = block_reason(known.judge(node.url, "enforce"))
                node.blocked = node.blocked if judged is None and node.blocked == "tdm" else judged
        blocked += node.blocked is not None
    origins = leaf_origins(tree)
    unchecked = tuple(o for o in origins if o in rules and (rules[o] is None or o in cut))
    return replace(
        tree,
        blocked=blocked,
        unchecked=unchecked,
        unchecked_more=sum(1 for o in origins if o not in rules),
    )
