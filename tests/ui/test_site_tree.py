"""The site tree of a ``site`` job (ruling P10-S20): built from its items by URL path segments,
merged by seen key, pages first, capped at ``UI_TREE_MAX_URLS``."""

from typing import Any

from tests.ui.site_items import page_item, pending_item, robots_item, sitemap_item
from text_extraction.envelope import ErrorCode
from text_extraction.ui.site_tree import MAX_SECTION_DEPTH, SiteTree, TreeNode, build_tree

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
