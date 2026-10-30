"""The follow rules of a page's links (ruling P9-S6). Pure: the cached compliance verdicts come
in as ``blocked``, judged by the runner (``Pipeline.blocked_from_cache``).

Each link gets a preliminary ``crawl``, first rule that matches:

- ``listed``: a head ``<link>`` or an ``<iframe>``, never followed;
- ``file``: a file by its extension;
- ``out_of_scope``: another host (a leading ``www.`` ignored), or outside ``scope: path``;
- ``nofollow``: its ``rel`` says ``nofollow``, or the page said ``nofollow``;
- ``blocked``: robots.txt or a TDM reservation blocks it under the job's mode, from the cached
  origin files (not cached yet: followed, and the item decides when it is fetched);
- ``depth``: the linked page would be deeper than the crawl's ``depth``;
- ``queued``: to be followed; ``SiteStore.extend`` decides whether it becomes a new item
  (``queued``), is an existing one (``seen``) or is over the cap (``max_pages``, or
  ``memory_guard`` while Valkey is nearly full).

``blocked`` and ``depth`` links are still looked up (a candidate with ``add`` False): a page that
has an item gets its ``target``, and the candidates of a page do not depend on the compliance
cache, which fills while the crawl runs. A rerun of the page sends the same candidates in the
same order, which the store's positional answers need (ruling R-P9-1).
"""

from collections.abc import Iterable, Mapping, Sequence

from text_extraction.site.store import Candidate
from text_extraction.site.types import Crawl, SiteLink, SiteNode, SiteScope
from text_extraction.site.urls import in_scope, normalize_url, seen_key

__all__ = ["judge_links", "unique_candidates"]

_LISTED_SOURCES = frozenset({"link", "iframe"})


def judge_links(
    links: Sequence[SiteLink],
    *,
    page_nofollow: bool,
    scope: SiteScope,
    child_depth: int,
    max_depth: int,
    blocked: Mapping[str, bool | None],
    parent: int,
) -> list[tuple[SiteLink, Candidate | None]]:
    """Each link with its preliminary ``crawl`` and its candidate, if any: a page item of depth
    ``child_depth`` found by item ``parent``, to append (``add``) or to look up only. Two links
    to one page (by ``seen_key``) have equal candidates; ``unique_candidates`` keeps the first.
    ``blocked`` holds the cached verdict by link URL (missing or None: not cached)."""
    judged: list[tuple[SiteLink, Candidate | None]] = []
    for link in links:
        crawl, follow = _rule(link, page_nofollow, scope, blocked)
        candidate: Candidate | None = None
        if follow:
            url = normalize_url(link.url) or link.url
            add = crawl == "queued" and child_depth <= max_depth
            if crawl == "queued" and not add:
                crawl = "depth"
            candidate = Candidate(
                key=seen_key(url),
                url=url,
                node=SiteNode(role="page", depth=child_depth, parent=parent),
                add=add,
            )
        judged.append((link.model_copy(update={"crawl": crawl, "target": None}), candidate))
    return judged


def _rule(
    link: SiteLink, page_nofollow: bool, scope: SiteScope, blocked: Mapping[str, bool | None]
) -> tuple[Crawl, bool]:
    """The link's preliminary crawl, and whether it is looked up in the crawl."""
    if link.source in _LISTED_SOURCES:
        return "listed", False
    if link.kind == "file":
        return "file", False
    if not in_scope(link.url, scope):
        return "out_of_scope", False
    if page_nofollow or "nofollow" in link.rel:
        return "nofollow", False
    if blocked.get(link.url) is True:
        return "blocked", True
    return "queued", True


def unique_candidates(candidates: Iterable[Candidate | None]) -> list[Candidate]:
    """The candidates without None and without a second one of a key, in order."""
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate is not None and candidate.key not in unique:
            unique[candidate.key] = candidate
    return list(unique.values())
