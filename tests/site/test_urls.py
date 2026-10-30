"""URLs, seen keys and the crawl's scope (ruling P9-S4)."""

import pytest

from text_extraction.site.types import SiteScope
from text_extraction.site.urls import in_scope, normalize_url, scope_for, seen_key, site_host


def test_normalize_url() -> None:
    assert normalize_url("HTTPS://Example.ORG:443/a#x") == "https://example.org/a"
    assert normalize_url("http://example.org") == "http://example.org/"
    assert normalize_url("mailto:x@y") is None
    long_url = "https://example.org/" + "a" * (2049 - len("https://example.org/"))
    assert len(long_url) == 2049
    assert normalize_url(long_url) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.org:80/a", "http://example.org/a"),
        ("http://example.org:443/a", "http://example.org:443/a"),
        ("https://example.org:8443", "https://example.org:8443/"),
        ("https://example.org/A%2Fb?Q=%20x&y=1", "https://example.org/A%2Fb?Q=%20x&y=1"),
        ("  https://example.org/a  ", "https://example.org/a"),
        ("https://[2001:DB8::1]:443/a", "https://[2001:db8::1]/a"),
        ("https://example.org?x=1", "https://example.org/?x=1"),
    ],
)
def test_normalize_url_keeps_query_and_encoding(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "/relative/path",
        "ftp://example.org/a",
        "javascript:alert(1)",
        "https://",
        "https://user:pw@example.org/",
        "https://example.org:99999/",
        "https://example.org:0/",
        "http://[::1/",
        "",
    ],
)
def test_normalize_url_refuses(url: str) -> None:
    assert normalize_url(url) is None


def test_seen_key_ignores_scheme_and_www() -> None:
    assert seen_key("https://www.a.example/p?q=1") == seen_key("http://a.example/p?q=1")
    assert seen_key("https://a.example/p?q=1") != seen_key("https://a.example/p?q=2")
    assert seen_key("https://A.example:443/p#x") == seen_key("https://a.example/p")
    assert seen_key("https://a.example:8080/p") != seen_key("https://a.example/p")


def test_site_host() -> None:
    assert site_host("https://WWW.A.example:8080/x") == "a.example"
    assert site_host("https://a.example/") == "a.example"
    assert site_host("not a url") == ""


def test_scope_host() -> None:
    scope = scope_for("https://www.a.example/mathe/index.html", None, "host")

    assert scope == SiteScope(host="a.example", path_prefix=None)
    assert in_scope("https://a.example/physik/", scope)
    assert in_scope("http://www.a.example/", scope)
    assert not in_scope("https://b.example/", scope)
    assert not in_scope("https://sub.a.example/", scope)


def test_scope_path_prefix() -> None:
    scope = scope_for("https://a.example/mathe/index.html", None, "path")

    assert scope.path_prefix == "/mathe/"
    assert in_scope("https://a.example/mathe/b", scope)
    assert not in_scope("https://a.example/physik/", scope)
    assert in_scope("https://www.a.example/mathe/x", scope)
    assert not in_scope("https://a.example/mathematik", scope)


def test_scope_follows_the_final_url() -> None:
    scope = scope_for("https://a.example/m", "https://www.b.example/kurs/start", "path")

    assert scope == SiteScope(host="b.example", path_prefix="/kurs/")


@pytest.mark.parametrize(
    ("url", "prefix"),
    [("https://a.example", "/"), ("https://a.example/", "/"), ("https://a.example/m/", "/m/")],
)
def test_scope_path_prefix_of_short_paths(url: str, prefix: str) -> None:
    assert scope_for(url, None, "path").path_prefix == prefix
