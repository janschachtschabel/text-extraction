"""The site tree of a ``site`` job (ruling P10-S20): built from its items by URL path segments,
merged by seen key, pages first, capped at ``UI_TREE_MAX_URLS``."""

import asyncio
import time
from typing import Any, cast

from fakeredis import FakeAsyncRedis

from tests.test_compliance import FakeFetcher, result
from tests.ui.site_items import page_item, pending_item, robots_item, sitemap_item
from text_extraction.compliance import ComplianceChecker, ComplianceReport, OriginRules
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import Fetcher
from text_extraction.job_models import JobItem
from text_extraction.settings import Settings
from text_extraction.ui.site_tree import (
    MAX_SECTION_DEPTH,
    TREE_MAX_SOCIAL,
    SiteTree,
    TreeNode,
    build_tree,
    leaf_origins,
    mark_blocked,
)

ORIGIN = "https://example.org"


def shape(node: TreeNode) -> tuple[Any, ...]:
    """A node as (label, kind, url, title, status, depth, lastmod, children, error_code),
    recursively."""
    return (
        node.label,
        node.kind,
        node.url,
        node.title,
        node.status,
        node.depth,
        node.lastmod,
        [shape(child) for child in node.children],
        node.error_code,
    )


def leaf(label: str, kind: str, url: str, **fields: Any) -> tuple[Any, ...]:
    return (
        label,
        kind,
        url,
        fields.get("title"),
        fields.get("status"),
        fields.get("depth"),
        fields.get("lastmod"),
        [],
        fields.get("error_code"),
    )


def section(label: str, *children: tuple[Any, ...]) -> tuple[Any, ...]:
    return (label, "section", None, None, None, None, None, list(children), None)


def test_tree_by_path_segments() -> None:
    items = [
        page_item(
            0,
            f"{ORIGIN}/",
            title="Home",
            depth=0,
            links=(
                (f"{ORIGIN}/deep/x", "depth"),
                (f"{ORIGIN}/deep/x", "max_pages"),  # the same URL again: one node
                (f"{ORIGIN}/docs/intro", "queued"),  # has an item of its own
                ("https://other.example/", "out_of_scope"),
                (f"{ORIGIN}/file.pdf", "file"),
            ),
        ),
        robots_item(1, f"{ORIGIN}/robots.txt"),
        page_item(2, f"{ORIGIN}/docs/intro", title="Intro"),
        page_item(3, f"{ORIGIN}/docs/", error=ErrorCode.TARGET_HTTP_ERROR),
        sitemap_item(
            4,
            f"{ORIGIN}/sitemap.xml",
            [
                # On www. of the same host: merged into the page, which gets its lastmod.
                {"loc": "https://www.example.org/docs/intro", "lastmod": "2026-01-01"},
                {"loc": f"{ORIGIN}/About", "lastmod": "2026-02-02"},
                {"loc": f"{ORIGIN}/docs/intro?lang=de"},
                {"loc": "http://example.org/About"},  # http and https are one page
            ],
        ),
        pending_item(5, f"{ORIGIN}/later"),
    ]

    tree = build_tree(items, max_urls=100)

    assert (tree.urls, tree.hidden) == (6, 0)
    assert [shape(root) for root in tree.roots] == [
        section(
            "example.org",
            leaf("/", "page", f"{ORIGIN}/", title="Home", status="success", depth=0),
            leaf("About", "sitemap", f"{ORIGIN}/About", lastmod="2026-02-02"),
            section("deep/", leaf("x", "linked", f"{ORIGIN}/deep/x")),
            section(
                "docs/",
                leaf(
                    "/",
                    "page",
                    f"{ORIGIN}/docs/",
                    status="failed",
                    error_code="target_http_error",
                    depth=1,
                ),
                leaf(
                    "intro",
                    "page",
                    f"{ORIGIN}/docs/intro",
                    title="Intro",
                    status="success",
                    depth=1,
                    lastmod="2026-01-01",
                ),
                leaf("intro?lang=de", "sitemap", f"{ORIGIN}/docs/intro?lang=de"),
            ),
        )
    ]


def test_tree_pages_come_first_and_each_source_counts() -> None:
    entries = [{"loc": f"{ORIGIN}/s/{number}"} for number in range(5)]
    items = [
        sitemap_item(0, f"{ORIGIN}/sitemap.xml", entries),
        # A page found after the entries is still shown before them, and one listed in the
        # sitemap is one URL.
        page_item(1, f"{ORIGIN}/p/1", links=((f"{ORIGIN}/l/1", "depth"),)),
        page_item(2, f"{ORIGIN}/s/0"),
    ]

    tree = build_tree(items, max_urls=3)

    assert (tree.urls, tree.hidden) == (3, 4)
    urls = [node.url for node in _leaves(tree)]
    assert sorted(urls, key=str) == [f"{ORIGIN}/p/1", f"{ORIGIN}/s/0", f"{ORIGIN}/s/1"]
    assert {node.url: node.kind for node in _leaves(tree)}[f"{ORIGIN}/s/0"] == "page"


def test_tree_hosts_and_deep_paths() -> None:
    deep = "/".join(f"d{level}" for level in range(MAX_SECTION_DEPTH + 5))
    items = [
        page_item(0, f"{ORIGIN}/", depth=0),
        page_item(1, "https://www.example.org:8443/a"),
        page_item(2, f"{ORIGIN}/{deep}/page"),
    ]

    tree = build_tree(items, max_urls=100)

    assert [root.label for root in tree.roots] == ["example.org", "example.org:8443"]
    node = tree.roots[0]
    depth = 0
    while node.children and node.children[-1].kind == "section":
        node = node.children[-1]
        depth += 1
    assert depth == MAX_SECTION_DEPTH
    rest = "/".join(f"d{level}" for level in range(MAX_SECTION_DEPTH, MAX_SECTION_DEPTH + 5))
    assert [child.label for child in node.children] == [f"{rest}/page"]


def _leaves(tree: SiteTree) -> list[TreeNode]:
    found: list[TreeNode] = []
    stack = list(tree.roots)
    while stack:
        node = stack.pop()
        if node.url is not None:
            found.append(node)
        stack.extend(node.children)
    return found


def test_tree_page_types_of_crawled_sitemap_and_linked_leaves() -> None:
    """A crawled page has its own type; a sitemap entry and a linked-only URL the type their URL
    says, guessed, and none when it says nothing: a link's category is not used (ruling
    P14-R8). A page without a type (failed, or stored before page types) stays without one,
    even when its URL says something (ruling P14-R7)."""
    items = [
        # Listed before the pages: the pages that move up keep their own verdict.
        sitemap_item(0, f"{ORIGIN}/sitemap.xml", [{"loc": f"{ORIGIN}/login"}]),
        page_item(
            1,
            f"{ORIGIN}/",
            depth=0,
            page_type="home",
            links=(
                (f"{ORIGIN}/impressum", "depth", "imprint"),
                (f"{ORIGIN}/docs/x", "max_pages", "content"),  # the URL says nothing: no type
                (f"{ORIGIN}/ueber-uns/team", "depth", "content"),  # the URL wins: about
                (f"{ORIGIN}/start", "depth", "nav"),
                (f"{ORIGIN}/kontakt", "depth"),  # stored before link categories
            ),
        ),
        page_item(2, f"{ORIGIN}/docs/a", page_type="content"),
        page_item(3, f"{ORIGIN}/docs/b", page_type="content"),
        page_item(4, f"{ORIGIN}/login", error=ErrorCode.TARGET_HTTP_ERROR),
        page_item(5, f"{ORIGIN}/datenschutz", page_type="privacy"),
        # Listed after the pages: merged into them, which keep their own verdict.
        sitemap_item(
            6,
            f"{ORIGIN}/sitemap.xml",
            [
                {"loc": f"{ORIGIN}/datenschutz"},
                {"loc": f"{ORIGIN}/docs/a"},
                {"loc": f"{ORIGIN}/agb"},
                {"loc": f"{ORIGIN}/de/kontakt"},
                {"loc": f"{ORIGIN}/news/2026"},
            ],
            part=2,
        ),
    ]

    tree = build_tree(items, max_urls=100)

    assert {node.url: (node.kind, node.page_type, node.type_guessed) for node in _leaves(tree)} == {
        f"{ORIGIN}/": ("page", "home", False),
        f"{ORIGIN}/docs/a": ("page", "content", False),
        f"{ORIGIN}/docs/b": ("page", "content", False),
        f"{ORIGIN}/login": ("page", None, False),
        f"{ORIGIN}/datenschutz": ("page", "privacy", False),
        f"{ORIGIN}/agb": ("sitemap", "legal", True),
        f"{ORIGIN}/de/kontakt": ("sitemap", "contact", True),
        f"{ORIGIN}/news/2026": ("sitemap", None, False),
        f"{ORIGIN}/impressum": ("linked", "imprint", True),
        f"{ORIGIN}/docs/x": ("linked", None, False),
        f"{ORIGIN}/ueber-uns/team": ("linked", "about", True),
        f"{ORIGIN}/start": ("linked", None, False),
        f"{ORIGIN}/kontakt": ("linked", "contact", True),
    }
    # Most first, then in the order of the types; guessed types count, leaves without a type
    # do not (/login, /news/2026, /docs/x, /start).
    assert list(tree.type_counts.items()) == [
        ("content", 2),
        ("contact", 2),
        ("home", 1),
        ("imprint", 1),
        ("privacy", 1),
        ("legal", 1),
        ("about", 1),
    ]
    assert sum(tree.type_counts.values()) == tree.urls - 4


def test_tree_counts_only_the_shown_leaves() -> None:
    items = [
        page_item(0, f"{ORIGIN}/a", page_type="content"),
        page_item(1, f"{ORIGIN}/impressum", page_type="imprint"),
        sitemap_item(2, f"{ORIGIN}/sitemap.xml", [{"loc": f"{ORIGIN}/agb"}]),
    ]

    tree = build_tree(items, max_urls=2)

    assert (tree.urls, tree.hidden) == (2, 1)
    assert dict(tree.type_counts) == {"content": 1, "imprint": 1}


def test_an_old_jobs_items_have_no_page_type() -> None:
    tree = build_tree([page_item(0, f"{ORIGIN}/impressum")], max_urls=10)

    [node] = _leaves(tree)
    assert (node.page_type, node.type_guessed) == (None, False)
    assert dict(tree.type_counts) == {}


# --- blocked leaves (package 16) ---------------------------------------------------------------


def _with_compliance(item: JobItem, **report: Any) -> JobItem:
    assert item.result is not None
    assert item.result.data is not None
    item.result.data.compliance = ComplianceReport.model_validate(report)
    return item


def _rules(url: str, robots: str, *, origin: str = ORIGIN) -> OriginRules:
    fetcher = FakeFetcher({f"{origin}/robots.txt": result(f"{origin}/robots.txt", robots)})
    checker = ComplianceChecker(
        cast(Fetcher, fetcher), FakeAsyncRedis(), Settings(auth_mode="none")
    )
    rules = asyncio.run(checker.origin_rules(url, cached_only=False))
    assert rules is not None
    return rules


def test_crawled_page_blocked_by_robots_is_marked() -> None:
    blocked = _with_compliance(
        page_item(0, f"{ORIGIN}/a", error=ErrorCode.BLOCKED_BY_ROBOTS),
        mode="enforce",
        decision="blocked",
        robots="disallowed",
    )
    allowed = page_item(1, f"{ORIGIN}/b")

    tree = build_tree([blocked, allowed], max_urls=10)

    marks = {node.url: node.blocked for node in _leaves(tree)}
    assert marks == {f"{ORIGIN}/a": "robots", f"{ORIGIN}/b": None}


def test_tree_records_the_crawl_mode() -> None:
    item = _with_compliance(page_item(0, f"{ORIGIN}/a"), mode="ignore", decision="skipped")

    assert build_tree([item], max_urls=10).crawl_mode == "ignore"
    assert build_tree([], max_urls=10).crawl_mode is None


def test_blocked_links_are_linked_only_leaves() -> None:
    items = [page_item(0, f"{ORIGIN}/", depth=0, links=((f"{ORIGIN}/x", "blocked"),))]

    tree = build_tree(items, max_urls=10)

    [node] = [one for one in _leaves(tree) if one.kind == "linked"]
    assert (node.url, node.blocked) == (f"{ORIGIN}/x", None)


def _blocked_fixture() -> SiteTree:
    items = [
        page_item(
            0, f"{ORIGIN}/sammlung/", depth=0, links=((f"{ORIGIN}/sammlung/_page3", "depth"),)
        ),
        sitemap_item(1, f"{ORIGIN}/sitemap.xml", [{"loc": f"{ORIGIN}/sammlung/_page2"}]),
    ]
    return build_tree(items, max_urls=10)


def test_mark_blocked_judges_sitemap_and_linked_leaves() -> None:
    tree = _blocked_fixture()
    rules = _rules(f"{ORIGIN}/", "User-agent: *\nDisallow: /sammlung/_page\n")

    marked = mark_blocked(tree, {ORIGIN: rules})

    marks = {node.url: node.blocked for node in _leaves(marked)}
    assert marks == {
        f"{ORIGIN}/sammlung/": None,
        f"{ORIGIN}/sammlung/_page2": "robots",
        f"{ORIGIN}/sammlung/_page3": "robots",
    }
    assert (marked.blocked, marked.unchecked) == (2, ())


def test_mark_blocked_names_origins_without_rules() -> None:
    other = "https://other.example"
    items = [
        page_item(0, f"{ORIGIN}/a", depth=0),
        sitemap_item(1, f"{other}/sitemap.xml", [{"loc": f"{other}/p"}]),
    ]
    tree = build_tree(items, max_urls=10)
    rules = _rules(f"{ORIGIN}/", "User-agent: *\nDisallow: /\n")

    marked = mark_blocked(tree, {ORIGIN: rules, other: None})

    marks = {node.url: node.blocked for node in _leaves(marked)}
    assert marks == {f"{ORIGIN}/a": "robots", f"{other}/p": None}
    assert (marked.blocked, marked.unchecked) == (1, (other,))
    # An origin that was not tried is counted, not named.
    not_tried = mark_blocked(tree, {ORIGIN: rules})
    assert (not_tried.unchecked, not_tried.unchecked_more) == ((), 1)


def test_leaf_origins_orders_by_leaf_count() -> None:
    items = [
        page_item(0, "https://b.example/1", depth=0),
        page_item(1, "https://a.example/1"),
        page_item(2, "https://b.example/2"),
        sitemap_item(3, "https://c.example/s.xml", [{"loc": "ftp://c.example/x"}]),
    ]

    tree = build_tree(items, max_urls=10)

    assert leaf_origins(tree) == {
        "https://b.example": "https://b.example/1",
        "https://a.example": "https://a.example/1",
    }


def test_mark_blocked_current_judgement_replaces_a_stale_crawl_reason() -> None:
    stale = _with_compliance(
        page_item(0, f"{ORIGIN}/a", error=ErrorCode.BLOCKED_BY_ROBOTS),
        mode="enforce",
        decision="blocked",
        robots="unavailable",
    )
    tree = build_tree([stale], max_urls=10)
    allowing = _rules(f"{ORIGIN}/", "User-agent: *\nDisallow: /other\n")

    marked = mark_blocked(tree, {ORIGIN: allowing})

    assert [node.blocked for node in _leaves(marked)] == [None]
    assert marked.blocked == 0


def test_mark_blocked_keeps_a_crawl_tdm_reason_and_a_reason_of_an_unchecked_origin() -> None:
    noai = _with_compliance(
        page_item(0, f"{ORIGIN}/a"), mode="enforce", decision="blocked", noai=True
    )
    other = "https://other.example"
    robots = _with_compliance(
        page_item(1, f"{other}/b"), mode="enforce", decision="blocked", robots="disallowed"
    )
    tree = build_tree([noai, robots], max_urls=10)
    allowing = _rules(f"{ORIGIN}/", "User-agent: *\nDisallow: /other\n")

    marked = mark_blocked(tree, {ORIGIN: allowing, other: None})

    marks = {node.url: node.blocked for node in _leaves(marked)}
    assert marks == {f"{ORIGIN}/a": "tdm", f"{other}/b": "robots"}


def test_mark_blocked_deadline_leaves_the_rest_unchecked() -> None:
    tree = _blocked_fixture()
    rules = _rules(f"{ORIGIN}/", "User-agent: *\nDisallow: /sammlung/_page\n")

    marked = mark_blocked(tree, {ORIGIN: rules}, deadline=time.monotonic() - 1)

    assert marked.unchecked == (ORIGIN,)
    assert marked.blocked == 0
    assert all(node.blocked is None for node in _leaves(marked))


# --- one row per lone page, untyped count, social links (package 17) ----------------------------


def test_section_with_only_its_page_is_one_row() -> None:
    sitemap = sitemap_item(
        0,
        "https://x.org/sitemap.xml",
        [{"loc": "https://x.org/a/"}, {"loc": "https://x.org/b/"}, {"loc": "https://x.org/b/c"}],
    )

    tree = build_tree([sitemap], max_urls=10)

    assert [shape(root) for root in tree.roots] == [
        section(
            "x.org",
            leaf("a/", "sitemap", "https://x.org/a/"),
            section(
                "b/",
                leaf("/", "sitemap", "https://x.org/b/"),
                leaf("c", "sitemap", "https://x.org/b/c"),
            ),
        )
    ]


def test_folded_row_keeps_blocked_and_type() -> None:
    page = page_item(0, "https://x.org/impressum/", page_type="imprint")
    tree = build_tree([page], max_urls=10)
    rules = _rules(
        "https://x.org/", "User-agent: *\nDisallow: /impressum/\n", origin="https://x.org"
    )

    marked = mark_blocked(tree, {"https://x.org": rules})

    [root] = marked.roots
    [node] = root.children
    assert (node.label, node.url, node.children) == ("impressum/", "https://x.org/impressum/", [])
    assert (node.kind, node.page_type, node.blocked) == ("page", "imprint", "robots")


def test_untyped_counts_unfetched_leaves_without_type() -> None:
    sitemap = sitemap_item(
        0,
        "https://x.org/sitemap.xml",
        [{"loc": "https://x.org/2-dezember/"}, {"loc": "https://x.org/impressum/"}],
    )
    failed = page_item(1, "https://x.org/broken", error=ErrorCode.TARGET_HTTP_ERROR)

    tree = build_tree([sitemap, failed], max_urls=10)

    assert tree.untyped == 1


def test_social_links_collected_once() -> None:
    page = page_item(
        0,
        "https://x.org/",
        external=(
            ("https://www.facebook.com/wlo", "social"),
            ("https://facebook.com/wlo", "social"),
            ("https://mastodon.social/@wlo", "social"),
            ("https://example.com/x", "content"),
        ),
    )

    tree = build_tree([page], max_urls=10)

    assert [(link.url, link.host) for link in tree.social] == [
        ("https://www.facebook.com/wlo", "facebook.com"),
        ("https://mastodon.social/@wlo", "mastodon.social"),
    ]


def test_social_links_capped() -> None:
    page = page_item(
        0,
        "https://x.org/",
        external=tuple((f"https://facebook.com/page{n}", "social") for n in range(60)),
    )

    tree = build_tree([page], max_urls=10)

    assert len(tree.social) == TREE_MAX_SOCIAL == 50


def test_social_links_use_category_or_host() -> None:
    page = page_item(
        0,
        "https://x.org/",
        external=(
            ("https://unknown-network.example/me", "social"),
            ("https://www.instagram.com/wlo", None),  # stored before categories: judged by host
            ("https://example.com/x", None),
        ),
    )

    tree = build_tree([page], max_urls=10)

    assert [link.host for link in tree.social] == ["unknown-network.example", "instagram.com"]
