from text_extraction.convert import Link, page_links
from text_extraction.convert.links import absolute_url

PAGE = """<html><head><base href="https://www.example.org/de/"></head><body>
<a href="intro">Intro</a>
<a href="https://example.org/about">About (www-less host)</a>
<a href="https://other.example.net/x?q=1">Elsewhere</a>
<a href="/files/Worksheet.PDF">Worksheet</a>
<a href="https://cdn.example.net/a.zip?dl=1"></a>
<a href="#top">In page</a>
<a href="mailto:a@example.org">Mail</a>
<a href="javascript:void(0)">Script</a>
<a href="tel:+49123">Call</a>
<a href="intro">Intro again</a>
<a href="/icon" aria-label="Settings"><svg></svg></a>
<a href="/titled" title="Titled link"></a>
<a href="http://[URL]">Template placeholder</a>
<a href="  /spaced  ">  Spaced
   out </a>
</body></html>"""


def test_page_links_kinds() -> None:
    links = page_links(PAGE, "https://www.example.org/de/start")

    assert links == [
        Link("https://www.example.org/de/intro", "Intro", "internal"),
        Link("https://example.org/about", "About (www-less host)", "internal"),
        Link("https://other.example.net/x?q=1", "Elsewhere", "external"),
        Link("https://www.example.org/files/Worksheet.PDF", "Worksheet", "file"),
        Link("https://cdn.example.net/a.zip?dl=1", "", "file"),
        Link("https://www.example.org/icon", "Settings", "internal"),
        Link("https://www.example.org/titled", "Titled link", "internal"),
        Link("https://www.example.org/spaced", "Spaced out", "internal"),
    ]


def test_page_links_without_links_or_html() -> None:
    assert page_links("", "https://example.org/") == []
    assert page_links("<p>No links</p>", "https://example.org/") == []


def test_page_links_resolve_against_the_page_without_a_base_tag() -> None:
    links = page_links('<a href="../up">Up</a>', "https://example.org/a/b/c")

    assert links == [Link("https://example.org/a/up", "Up", "internal")]


def test_absolute_url_returns_none_for_unparseable_references() -> None:
    assert absolute_url("https://example.org/", "http://[URL]") is None
    assert absolute_url("https://example.org/a/", "b") == "https://example.org/a/b"
