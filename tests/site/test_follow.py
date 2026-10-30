"""The follow rules of a page's links (ruling P9-S6)."""

from typing import Any

from text_extraction.site.follow import judge_links, unique_candidates
from text_extraction.site.store import Candidate
from text_extraction.site.types import SiteLink, SiteNode, SiteScope
from text_extraction.site.urls import seen_key

SCOPE = SiteScope(host="lernen.example.org", path_prefix=None)
ORIGIN = "https://lernen.example.org"


def link(url: str, **fields: Any) -> SiteLink:
    values: dict[str, Any] = {
        "url": url,
        "text": "",
        "kind": "internal",
        "source": "a",
        "rel": [],
        "crawl": "listed",
    }
    values.update(fields)
    return SiteLink(**values)


def judge(links: list[SiteLink], **options: Any) -> list[tuple[SiteLink, Candidate | None]]:
    values: dict[str, Any] = {
        "page_nofollow": False,
        "scope": SCOPE,
        "child_depth": 1,
        "max_depth": 2,
        "blocked": {},
        "parent": 0,
    }
    values.update(options)
    return judge_links(links, **values)


def test_crawl_values() -> None:
    links = [
        link(f"{ORIGIN}/canonical", source="link", rel=["canonical"]),
        link(f"{ORIGIN}/frame", source="iframe"),
        link(f"{ORIGIN}/worksheet.pdf", kind="file"),
        link("https://other.example/", kind="external"),
        link(f"{ORIGIN}/sponsored", rel=["sponsored", "nofollow"]),
        link(f"{ORIGIN}/intern/secret"),
        link(f"{ORIGIN}/next"),
    ]
    blocked = {f"{ORIGIN}/intern/secret": True, f"{ORIGIN}/next": None}

    judged = judge(links, blocked=blocked)

    assert [found.crawl for found, _ in judged] == [
        "listed",
        "listed",
        "file",
        "out_of_scope",
        "nofollow",
        "blocked",
        "queued",
    ]
    candidates = [candidate for _, candidate in judged]
    assert candidates[:5] == [None] * 5
    # A blocked link is looked up only: a page already queued for it still gets its target.
    assert candidates[5] == Candidate(
        key=seen_key(f"{ORIGIN}/intern/secret"),
        url=f"{ORIGIN}/intern/secret",
        node=SiteNode(role="page", depth=1, parent=0),
        add=False,
    )
    assert candidates[6] == Candidate(
        key=seen_key(f"{ORIGIN}/next"),
        url=f"{ORIGIN}/next",
        node=SiteNode(role="page", depth=1, parent=0),
        add=True,
    )


def test_page_nofollow_and_depth() -> None:
    [(nofollow, none)] = judge([link(f"{ORIGIN}/a")], page_nofollow=True)
    assert (nofollow.crawl, none) == ("nofollow", None)

    [(deep, lookup)] = judge([link(f"{ORIGIN}/a")], child_depth=3, max_depth=2)
    assert deep.crawl == "depth"
    assert lookup is not None
    assert not lookup.add  # looked up only, so a page that has an item gets its target
    assert lookup.node.depth == 3


def test_blocked_only_when_true() -> None:
    judged = judge(
        [link(f"{ORIGIN}/a"), link(f"{ORIGIN}/b")],
        blocked={f"{ORIGIN}/a": False, f"{ORIGIN}/b": None},
    )
    assert [found.crawl for found, _ in judged] == ["queued", "queued"]


def test_path_scope() -> None:
    scope = SiteScope(host="lernen.example.org", path_prefix="/kurs/")
    judged = judge([link(f"{ORIGIN}/kurs/a"), link(f"{ORIGIN}/blog/b")], scope=scope)
    assert [found.crawl for found, _ in judged] == ["queued", "out_of_scope"]


def test_www_and_scheme_are_one_candidate_in_link_order() -> None:
    links = [
        link(f"{ORIGIN}/a"),
        link("http://www.lernen.example.org/a"),
        link(f"{ORIGIN}/b"),
        link(f"{ORIGIN}/a#top"),
    ]

    judged = judge(links)
    unique = unique_candidates(candidate for _, candidate in judged)

    assert [candidate.url for candidate in unique] == [f"{ORIGIN}/a", f"{ORIGIN}/b"]
    assert all(candidate is not None for _, candidate in judged)


def test_unique_candidates_keeps_the_first_add() -> None:
    node = SiteNode(role="page", depth=1, parent=0)
    first = Candidate(key="k", url="https://x/", node=node, add=False)
    second = Candidate(key="k", url="https://x/", node=node, add=True)

    assert unique_candidates([first, None, second]) == [first]
