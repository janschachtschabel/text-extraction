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

Roots are hosts (a leading ``www.`` dropped, as in ``seen_key``); a section is a directory
(its label ends in ``/``) and has no URL of its own; every URL is a leaf: a directory's own
page is the leaf ``/`` inside it, and a query string makes a leaf of its own. Children are in
name order."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from text_extraction.job_models import JobItem
from text_extraction.site.types import SiteInfo
from text_extraction.site.urls import seen_key
from text_extraction.ui.i18n import Message

__all__ = [
    "FOUND_LABELS",
    "MAX_SECTION_DEPTH",
    "NodeKind",
    "SiteTree",
    "TreeBuilder",
    "TreeNode",
    "build_tree",
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
# Hidden URLs are counted once each up to this many (by their key's hash); beyond it, each
# further mention counts, so the count never needs more memory than this.
HIDDEN_KEYS_MAX: Final = 250_000
# Links not followed for these reasons are shown as linked only; a followed link has an item.
LINKED_ONLY: Final = frozenset({"depth", "max_pages"})
_PAGE, _ENTRY, _LINKED = 0, 1, 2
_KINDS: Final[tuple[NodeKind, ...]] = ("page", "sitemap", "linked")


@dataclass
class TreeNode:
    """One node: a section (no URL) or a URL leaf of ``kind`` ``page`` (crawled), ``sitemap``
    (listed only) or ``linked`` (linked only). ``status`` is a page's result status and
    ``error_code`` its error's code, ``depth`` its link distance from the start page,
    ``lastmod`` a sitemap's."""

    label: str
    url: str | None
    kind: NodeKind
    title: str | None = None
    status: str | None = None
    depth: int | None = None
    lastmod: str | None = None
    children: list["TreeNode"] = field(default_factory=list)
    error_code: str | None = None


@dataclass(frozen=True)
class SiteTree:
    """The roots (one per host), the URLs shown and how many more were not shown."""

    roots: list[TreeNode]
    urls: int
    hidden: int


@dataclass
class _Found:
    """What the sources say of one URL."""

    url: str
    title: str | None = None
    status: str | None = None
    depth: int | None = None
    lastmod: str | None = None
    error_code: str | None = None

    def merge(self, other: "_Found") -> None:
        self.title = self.title or other.title
        if self.status is None:
            self.status, self.error_code = other.status, other.error_code
        self.depth = self.depth if self.depth is not None else other.depth
        self.lastmod = self.lastmod or other.lastmod


class TreeBuilder:
    """Collects the URLs of a site job's items (`add`), at most ``max_urls`` per source, and
    builds the tree (`tree`)."""

    def __init__(self, max_urls: int) -> None:
        self._max = max_urls
        self._sources: tuple[dict[str, _Found], ...] = ({}, {}, {})
        self._hidden: set[int] = set()
        self._hidden_more = 0

    def add(self, item: JobItem) -> None:
        """Take what ``item`` says: a page, a sitemap's entries or a page's linked-only URLs.
        Items without a result (still queued) and robots.txt add nothing."""
        envelope = item.result
        data = envelope.data if envelope is not None else None
        site: SiteInfo | None = data.site if data is not None else None
        if envelope is None or data is None or site is None:
            return
        if site.role == "page" and item.url:
            code = str(envelope.error.code) if envelope.error is not None else None
            found = _Found(
                item.url,
                title=data.title,
                status=str(envelope.status),
                depth=site.depth,
                error_code=code,
            )
            self._add(_PAGE, found)
            for link in site.links or []:
                if link.crawl in LINKED_ONLY:
                    self._add(_LINKED, _Found(link.url))
        elif site.role in ("sitemap", "sitemap_part") and site.sitemap is not None:
            for entry in site.sitemap.entries:
                self._add(_ENTRY, _Found(entry.loc, lastmod=entry.lastmod))

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
        return SiteTree(roots=_assemble(shown), urls=len(shown), hidden=hidden)


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
            )
        )
    ordered = sorted(roots.values(), key=_order)
    stack = list(ordered)
    while stack:
        node = stack.pop()
        node.children.sort(key=_order)
        stack.extend(child for child in node.children if child.children)
    return ordered


def _order(node: TreeNode) -> tuple[str, str]:
    return node.label.casefold(), node.label
