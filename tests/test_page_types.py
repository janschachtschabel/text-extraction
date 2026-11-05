import json
import time
from typing import Literal

import pytest

from text_extraction.page_types import (
    LISTING_LINK_SHARE,
    LISTING_MIN_LINKS,
    PageSignal,
    PageType,
    link_category,
    page_type,
    url_type,
)
from text_extraction.quality import THIN_PAGE_BELOW_WORDS, Quality

MIN_WORDS = 50


def make_quality(
    *, words: int = 0, soft_404: bool = False, password: bool = False, **extra: bool
) -> Quality:
    return Quality(
        visible_chars=words * 6,
        word_count=words,
        script_count=0,
        js_heavy=False,
        bot_challenge=False,
        soft_404=soft_404,
        login_wall=password,
        browser_recommended=soft_404,
        **extra,
    )


def classify(
    html: str,
    url: str = "https://example.org/some/page",
    *,
    status: int | None = 200,
    words: int = 0,
    soft_404: bool = False,
    min_words: int = MIN_WORDS,
) -> tuple[PageType, list[PageSignal]]:
    info = page_type(
        html,
        url,
        status=status,
        quality=make_quality(words=words, soft_404=soft_404),
        min_words=min_words,
    )
    return info.type, info.signals


def doc(*, title: str = "", body: str = "", head: str = "") -> str:
    title_tag = f"<title>{title}</title>" if title else ""
    return f"<html><head>{title_tag}{head}</head><body>{body}</body></html>"


# --- url_type -----------------------------------------------------------------------------------

URL_TYPES: list[tuple[str, PageType | None]] = [
    # home
    ("https://example.org", "home"),
    ("https://example.org/", "home"),
    ("https://example.org/index.html", "home"),
    ("https://example.org/index.htm", "home"),
    ("https://example.org/INDEX.PHP", "home"),
    ("https://example.org/?utm_source=x", "home"),
    # home needs an empty query besides tracking parameters (A26-R1)
    ("https://example.org/?utm_source=x&fbclid=y", "home"),
    ("https://example.org/?ref=nav", "home"),
    ("https://example.org/?p=12", None),
    ("https://example.org/?page_id=42", None),
    ("https://example.org/index.php?id=42", None),
    ("https://example.org/index.html#top", "home"),
    ("https://example.org/de", "home"),
    ("https://example.org/en/", "home"),
    ("https://example.org/de-at/", "home"),
    ("https://example.org/en-GB/", "home"),
    ("https://example.org/de/index.html", "home"),
    ("https://example.org/uk/", "home"),  # Ukrainian
    ("https://example.org/hr", "home"),  # Croatian
    ("https://example.org/pt_BR/", "home"),
    # two letters that are not a site language (P14 final review)
    ("https://example.org/ki/", None),
    ("https://example.org/ai/", None),
    ("https://example.org/ui/", None),
    ("https://example.org/xx-de/", None),
    ("https://example.org/de/impressum", "imprint"),
    ("https://example.org/deindex.html", None),
    ("https://example.org/deu/", None),
    ("https://example.org/imprint\u0301", None),
    ("https://example.org/datenschutzerkla\u0308rung", "privacy"),
    ("https://example.org/impressum", "imprint"),
    ("https://example.org/impressum/", "imprint"),
    ("https://example.org/Impressum.html", "imprint"),
    ("https://example.org/de/impressum.php", "imprint"),
    ("https://example.org/imprint", "imprint"),
    ("https://example.org/legal-notice", "imprint"),
    ("https://example.org/site-notice", "imprint"),
    ("https://example.org/anbieterkennzeichnung", "imprint"),
    # privacy
    ("https://example.org/datenschutz", "privacy"),
    ("https://example.org/datenschutzerklaerung", "privacy"),
    ("https://example.org/datenschutzerkl%C3%A4rung", "privacy"),
    ("https://example.org/datenschutzerklärung.html", "privacy"),
    ("https://example.org/datenschutzhinweise", "privacy"),
    ("https://example.org/privacy", "privacy"),
    ("https://example.org/en/privacy-policy/", "privacy"),
    ("https://example.org/privacy_policy", "privacy"),
    ("https://example.org/privacy-notice", "privacy"),
    ("https://example.org/dsgvo", "privacy"),
    ("https://example.org/gdpr", "privacy"),
    # legal
    ("https://example.org/agb", "legal"),
    ("https://example.org/terms", "legal"),
    ("https://example.org/terms-of-service", "legal"),
    ("https://example.org/terms-of-use", "legal"),
    ("https://example.org/nutzungsbedingungen", "legal"),
    ("https://example.org/cookie-policy", "legal"),
    ("https://example.org/cookie-richtlinie", "legal"),
    ("https://example.org/haftungsausschluss", "legal"),
    ("https://example.org/disclaimer", "legal"),
    ("https://example.org/widerrufsrecht", "legal"),
    ("https://example.org/barrierefreiheit", "legal"),
    ("https://example.org/erklaerung-zur-barrierefreiheit", "legal"),
    ("https://example.org/erklärung-zur-barrierefreiheit", "legal"),
    ("https://example.org/accessibility-statement", "legal"),
    # contact
    ("https://example.org/kontakt", "contact"),
    ("https://example.org/Kontakt/", "contact"),
    ("https://example.org/contact", "contact"),
    ("https://example.org/contact-us", "contact"),
    ("https://example.org/kontaktformular.php", "contact"),
    # about
    ("https://example.org/ueber-uns", "about"),
    ("https://example.org/über-uns", "about"),
    ("https://example.org/%C3%BCber-uns", "about"),
    ("https://example.org/uber-uns", "about"),
    ("https://example.org/about", "about"),
    ("https://example.org/about-us/", "about"),
    ("https://example.org/wir-ueber-uns", "about"),
    ("https://example.org/team", "about"),
    ("https://example.org/wer-wir-sind", "about"),
    ("https://example.org/who-we-are", "about"),
    # auth
    ("https://example.org/login", "auth"),
    ("https://example.org/user/logout", "auth"),
    ("https://example.org/sign-in", "auth"),
    ("https://example.org/signup", "auth"),
    ("https://example.org/registrieren", "auth"),
    ("https://example.org/anmelden", "auth"),
    ("https://example.org/mein-konto", "auth"),
    ("https://example.org/my-account", "auth"),
    ("https://example.org/profil", "auth"),
    ("https://example.org/password-reset", "auth"),
    # search
    ("https://example.org/search", "search"),
    ("https://example.org/suche", "search"),
    ("https://example.org/recherche", "search"),
    ("https://example.org/?s=tomaten", "search"),
    ("https://example.org/?q=tomaten", "search"),
    ("https://example.org/find?query=tomaten", "search"),
    ("https://example.org/blog?search=tomaten", "search"),
    ("https://example.org/blog?suche=tomaten", "search"),
    ("https://example.org/blog?keyword=tomaten", "search"),
    # listing
    ("https://example.org/kategorie/rezepte", "listing"),
    ("https://example.org/category/news", "listing"),
    ("https://example.org/tag/python", "listing"),
    ("https://example.org/schlagwort/python", "listing"),
    ("https://example.org/archiv", "listing"),
    ("https://example.org/archive/2019", "listing"),
    ("https://example.org/blog/page/2", "listing"),
    ("https://example.org/blog/seite/3/", "listing"),
    ("https://example.org/blog?page=2", "listing"),
    ("https://example.org/blog?seite=4", "listing"),
    ("https://example.org/blog/category/news/page/2", "listing"),
    # a listing name counts with at most one segment after it (A26-R2)
    ("https://example.org/kategorie/mathe/bruchrechnung-einfach-erklaert", None),
    ("https://example.org/archiv/2019/artikel-titel", None),
    ("https://example.org/category/news/2024/05/headline", None),
    ("https://example.org/magazine/archive/2019/05/slug/588011/", None),
    # other names count in the first or the last segment, ambiguous ones only alone (A26-R2)
    ("https://example.org/rezepte/cookies", None),
    ("https://example.org/recipes/cookies", None),
    ("https://example.org/glossary/terms", None),
    ("https://example.org/services/legal", None),
    ("https://example.org/girokonto/konto", None),
    ("https://example.org/profile/jan", None),
    ("https://example.org/themen/recherche", None),
    ("https://example.org/cookies", "legal"),
    ("https://example.org/de/terms", "legal"),
    ("https://example.org/service/impressum/index.html", "imprint"),
    ("https://example.org/ueber-uns/kontakt", "contact"),
    # no type
    ("https://example.org/blog/mein-artikel", None),
    ("https://example.org/impressum-archiv-2019", None),
    ("https://example.org/blog/team-building-tipps", None),
    ("https://example.org/blog?s=", None),
    ("https://example.org/blog?s=&q=%20", None),
    ("https://example.org/?s=", "home"),
    ("https://example.org/?s=%20&p=", "home"),
    ("https://example.org/?UTM_Medium=mail&_ga=1.2", "home"),
    ("https://example.org/blog?page=abc", None),
    ("https://example.org/blog/page", None),
    ("https://example.org/pages/2", None),
    ("https://example.org/imprinting-in-ducks", None),
    ("https://example.org/products/12345", None),
    ("https://example.org/produkte?farbe=rot", None),
    # precedence when several match
    ("https://example.org/impressum/privacy", "imprint"),
    ("https://example.org/privacy/terms", "privacy"),
    ("https://example.org/terms/login", "auth"),
    ("https://example.org/login/search", "auth"),
    ("https://example.org/search/kontakt", "search"),
    ("https://example.org/kontakt/team", "contact"),
    ("https://example.org/team/archiv", "listing"),
    ("https://example.org/kontakt?s=x", "search"),
    ("https://example.org/impressum?page=2", "imprint"),
    # not a URL at all
    ("", "home"),
    ("/impressum", "imprint"),
    ("http://[::1", None),
]


@pytest.mark.parametrize(("url", "expected"), URL_TYPES)
def test_url_type(url: str, expected: PageType | None) -> None:
    assert url_type(url) == expected


def test_the_url_table_is_large_enough() -> None:
    assert len(URL_TYPES) >= 40


# --- link_category ------------------------------------------------------------------------------

LinkKind = Literal["internal", "external", "file"]

LINK_CATEGORIES: list[tuple[str, str, LinkKind, str]] = [
    ("https://example.org/", "Startseite", "internal", "home"),
    ("https://example.org/en/", "English", "internal", "home"),
    ("https://example.org/de-at", "Deutsch", "internal", "home"),
    ("https://example.org/index.html", "Home", "internal", "home"),
    ("https://example.org/impressum", "Impressum", "internal", "imprint"),
    ("https://example.org/datenschutz", "Datenschutz", "internal", "privacy"),
    ("https://example.org/agb", "AGB", "internal", "legal"),
    ("https://example.org/kontakt", "Kontakt", "internal", "contact"),
    ("https://example.org/ueber-uns", "Über uns", "internal", "about"),
    ("https://example.org/login", "Anmelden", "internal", "auth"),
    ("https://example.org/suche?q=a", "Suche", "internal", "search"),
    ("https://example.org/kategorie/a", "Rezepte", "internal", "listing"),
    ("https://example.org/blog/artikel", "Ein Artikel", "internal", "content"),
    # the URL wins over the text
    ("https://example.org/impressum", "Startseite", "internal", "imprint"),
    # nav by text, internal links only after the URL
    ("https://example.org/blog/artikel", "Startseite", "internal", "nav"),
    ("https://example.org/blog/artikel", "  Nach   oben ", "internal", "nav"),
    ("https://example.org/blog/artikel", "Back to Top", "internal", "nav"),
    ("https://example.org/blog/artikel", "MENÜ", "internal", "nav"),
    ("https://example.org/blog/artikel", "Übersicht", "internal", "nav"),
    ("https://example.org/blog/artikel", "Sitemap", "internal", "nav"),
    ("https://example.org/blog/artikel", "»", "internal", "nav"),
    ("https://example.org/blog/artikel", "▲", "internal", "nav"),
    ("https://example.org/blog/artikel", "Zurück zur Übersicht der Rezepte", "internal", "content"),
    # downloads
    ("https://example.org/files/a.pdf", "Bericht", "file", "download"),
    ("https://other.org/impressum", "Impressum", "file", "download"),
    # social, including subdomains, only for external links
    ("https://www.facebook.com/firma", "Facebook", "external", "social"),
    ("https://de-de.facebook.com/firma", "", "external", "social"),
    ("https://x.com/firma", "", "external", "social"),
    ("https://mobile.twitter.com/firma", "", "external", "social"),
    ("https://youtu.be/abc", "", "external", "social"),
    ("https://t.me/firma", "", "external", "social"),
    ("https://github.com/firma/repo", "", "external", "social"),
    ("https://discord.gg/abc", "", "external", "social"),
    ("https://mastodon.social/@firma", "", "external", "social"),
    ("https://max.com/watch", "", "external", "content"),
    ("https://notfacebook.com/", "", "external", "content"),
    ("https://facebook.com.evil.org/", "", "external", "content"),
    # an external link says nothing about this site: no url_type, no nav
    ("https://other.org/impressum", "Impressum", "external", "content"),
    ("https://other.org/", "Startseite", "external", "content"),
    ("https://other.org/artikel", "Home", "external", "content"),
]


@pytest.mark.parametrize(("url", "text", "kind", "expected"), LINK_CATEGORIES)
def test_link_category(url: str, text: str, kind: LinkKind, expected: str) -> None:
    assert link_category(url, text, kind) == expected


# --- page_type ----------------------------------------------------------------------------------


def test_status_error_beats_everything() -> None:
    assert classify(doc(title="Kontakt"), "https://example.org/", status=404) == (
        "error",
        ["status"],
    )
    assert classify(doc(), "https://example.org/impressum", status=500, words=500)[0] == "error"


def test_soft_404_is_an_error() -> None:
    assert classify(doc(title="Kontakt"), "https://example.org/kontakt", soft_404=True) == (
        "error",
        ["soft_404"],
    )


def test_status_and_soft_404_are_both_listed() -> None:
    assert classify(doc(), status=410, soft_404=True) == ("error", ["status", "soft_404"])


def test_status_below_400_and_unknown_status_are_no_error() -> None:
    assert classify(doc(), status=301, words=100)[0] == "content"
    assert classify(doc(), status=None, words=100)[0] == "content"


def test_language_root_is_home() -> None:
    assert classify(doc(title="Kontakt"), "https://example.org/en/") == ("home", ["root_path"])
    assert classify(doc(), "https://example.org/de-AT", words=900)[0] == "home"


def html_lang(lang: str | None, *, title: str = "") -> str:
    attribute = f' lang="{lang}"' if lang is not None else ""
    return doc(title=title).replace("<html>", f"<html{attribute}>", 1)


def test_language_path_with_another_html_lang_is_no_home() -> None:
    article = html_lang("de", title="Ein Artikel")
    assert classify(article, "https://example.org/it/", words=900) == ("content", ["word_count"])
    assert classify(html_lang("de-DE"), "https://example.org/it/index.html", words=900)[0] == (
        "content"
    )


def test_language_path_with_its_own_html_lang_is_home() -> None:
    assert classify(html_lang("it-IT"), "https://example.org/it/", words=900) == (
        "home",
        ["root_path"],
    )
    assert classify(html_lang("IT"), "https://example.org/it")[0] == "home"
    assert classify(html_lang("pt-BR"), "https://example.org/pt_BR/")[0] == "home"
    assert classify(html_lang("de_AT"), "https://example.org/de-at/")[0] == "home"


def test_language_path_without_html_lang_is_home() -> None:
    assert classify(html_lang(None), "https://example.org/it/", words=900)[0] == "home"
    assert classify(html_lang(""), "https://example.org/it/", words=900)[0] == "home"


def test_root_path_is_home_whatever_the_html_lang() -> None:
    assert classify(html_lang("de"), "https://example.org/")[0] == "home"
    assert classify(html_lang("de"), "https://example.org/index.html")[0] == "home"


def test_root_path_is_home_even_with_a_contact_page_title() -> None:
    assert classify(doc(title="Kontakt"), "https://example.org/") == ("home", ["root_path"])
    assert classify(doc(), "https://example.org/index.php", words=900)[0] == "home"


def test_home_with_a_search_query_is_a_search() -> None:
    assert classify(doc(), "https://example.org/?s=x") == ("search", ["url"])


@pytest.mark.parametrize(
    ("schema_type", "expected"),
    [
        ("ContactPage", "contact"),
        ("AboutPage", "about"),
        ("SearchResultsPage", "search"),
        ("CollectionPage", "listing"),
    ],
)
def test_json_ld_page_type_beats_the_url(schema_type: str, expected: str) -> None:
    html = doc(
        head=f'<script type="application/ld+json">{{"@type": "{schema_type}"}}</script>',
        title="Willkommen",
    )
    assert classify(html, "https://example.org/service") == (expected, ["schema_org"])


def test_json_ld_beats_an_imprint_url() -> None:
    html = doc(head='<script type="application/ld+json">{"@type": "ContactPage"}</script>')
    assert classify(html, "https://example.org/impressum")[0] == "contact"


def test_json_ld_in_a_graph_and_with_type_lists() -> None:
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": "x"},
            {"@type": ["WebPage", "https://schema.org/AboutPage"], "name": "y"},
        ],
    }
    html = doc(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')
    assert classify(html, "https://example.org/firma") == ("about", ["schema_org"])


def test_json_ld_array_of_nodes() -> None:
    data = [{"@type": "WebSite"}, {"@type": "schema:SearchResultsPage"}]
    html = doc(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')
    assert classify(html, "https://example.org/firma")[0] == "search"


def test_json_ld_page_types_have_a_fixed_precedence() -> None:
    data = [{"@type": "CollectionPage"}, {"@type": "ContactPage"}]
    html = doc(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')
    assert classify(html)[0] == "contact"


def test_json_ld_that_is_not_a_page_type_is_ignored() -> None:
    html = doc(
        head='<script type="application/ld+json">{"@type": "Article"}</script>', title="Artikel"
    )
    assert classify(html, words=200) == ("content", ["word_count"])


def test_malformed_and_nested_json_ld_is_ignored() -> None:
    broken = doc(head='<script type="application/ld+json">{"@type": "ContactPage"</script>')
    assert classify(broken)[0] == "other"
    deep = "[" * 100_000 + "]" * 100_000
    nested = doc(head=f'<script type="application/ld+json">{deep}</script>')
    assert classify(nested)[0] == "other"


def test_oversized_json_ld_is_not_parsed() -> None:
    padding = " " * 2_000_000
    html = doc(
        head=f'<script type="application/ld+json">{{"@type": "ContactPage"{padding}}}</script>'
    )
    assert classify(html)[0] == "other"


def test_json_ld_page_type_inside_a_nested_object_is_not_the_main_entity() -> None:
    data = {"@type": "WebPage", "mainEntity": {"@type": "ContactPage"}}
    html = doc(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')
    assert classify(html)[0] == "other"


def test_microdata_itemtype_of_the_outermost_scope() -> None:
    html = doc(body='<div itemscope itemtype="https://schema.org/ContactPage"><p>x</p></div>')
    assert classify(html) == ("contact", ["schema_org"])


def test_microdata_ignores_nested_scopes() -> None:
    html = doc(
        body=(
            '<div itemscope itemtype="https://schema.org/WebPage">'
            '<div itemscope itemtype="https://schema.org/ContactPage"></div></div>'
        )
    )
    assert classify(html)[0] == "other"


def test_microdata_with_several_itemtypes() -> None:
    html = doc(
        body=(
            '<div itemscope itemtype="http://schema.org/WebPage http://schema.org/AboutPage"></div>'
        )
    )
    assert classify(html)[0] == "about"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org/impressum", "imprint"),
        ("https://example.org/datenschutz", "privacy"),
        ("https://example.org/agb", "legal"),
        ("https://example.org/login", "auth"),
        ("https://example.org/suche", "search"),
        ("https://example.org/kontakt", "contact"),
        ("https://example.org/ueber-uns", "about"),
        ("https://example.org/kategorie/news", "listing"),
    ],
)
def test_url_type_decides_when_nothing_stronger_applies(url: str, expected: str) -> None:
    assert classify(doc(title="Etwas anderes"), url, words=500) == (expected, ["url"])


def test_url_beats_title() -> None:
    assert classify(doc(title="Kontakt"), "https://example.org/impressum")[0] == "imprint"


def test_later_signals_that_agree_are_added() -> None:
    html = doc(title="Impressum | Firma", body="<h1>Impressum</h1>")
    assert classify(html, "https://example.org/impressum") == ("imprint", ["url", "title", "h1"])


def test_signals_that_disagree_are_left_out() -> None:
    html = doc(title="Kontakt", body="<h1>Impressum</h1>")
    assert classify(html, "https://example.org/impressum") == ("imprint", ["url", "h1"])


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Impressum", "imprint"),
        ("Imprint", "imprint"),
        ("Datenschutz", "privacy"),
        ("Datenschutzerklärung", "privacy"),
        ("Datenschutzerklaerung", "privacy"),
        ("Privacy Policy", "privacy"),
        ("AGB", "legal"),
        ("Nutzungsbedingungen", "legal"),
        ("Terms of Use", "legal"),
        ("Kontakt", "contact"),
        ("Contact", "contact"),
        ("Über uns", "about"),
        ("About us", "about"),
        ("Anmelden", "auth"),
        ("Login", "auth"),
        ("Registrieren", "auth"),
        ("Suche", "search"),
        ("Search results", "search"),
        ("Suchergebnisse", "search"),
        ("  impressum  ", "imprint"),
    ],
)
def test_title_and_first_h1_name_the_type(heading: str, expected: str) -> None:
    assert classify(doc(title=heading)) == (expected, ["title"])
    assert classify(doc(body=f"<h1>{heading}</h1>")) == (expected, ["h1"])


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Impressum | Firma GmbH", "imprint"),
        ("Impressum - Firma GmbH", "imprint"),
        ("Kontakt \u2013 Firma", "contact"),
        ("Kontakt \u2014 Firma", "contact"),
        ("Privacy Policy · Firma", "privacy"),
        ("Search results | Shop", "search"),
    ],
)
def test_title_may_start_with_the_type_word_and_a_separator(heading: str, expected: str) -> None:
    assert classify(doc(title=heading)) == (expected, ["title"])
    assert classify(doc(body=f"<h1>{heading}</h1>"))[0] == "other"


@pytest.mark.parametrize(
    "heading", ["Datenschutz: Was Schulen wissen müssen", "Suche: Tomaten", "Kontakt : Formular"]
)
def test_title_with_a_colon_subtitle_is_no_type(heading: str) -> None:
    assert classify(doc(title=heading))[0] == "other"
    html = doc(title=heading, body=f"<h1>{heading}</h1>")
    assert classify(html, "https://example.org/blog/x", words=900) == ("content", ["word_count"])


def test_h1_votes_only_as_the_whole_type_word() -> None:
    assert classify(doc(body="<h1>Kontakt</h1>")) == ("contact", ["h1"])
    assert classify(doc(body="<h1> Kontakt : </h1>")) == ("contact", ["h1"])
    assert classify(doc(body="<h1>Kontakt zur Schule aufnehmen</h1>"))[0] == "other"
    assert classify(doc(body="<h1>Kontakt | Schule</h1>"))[0] == "other"


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Firma | Impressum", "imprint"),
        ("ACME \u2013 Kontakt", "contact"),
        ("ACME - Kontakt", "contact"),
        ("Firma GmbH: Datenschutzerkl\u00e4rung", "privacy"),
        ("Firma \u00b7 Privacy Policy", "privacy"),
        ("Firma | Shop | Impressum", "imprint"),
        ("Acme Corp | About us", "about"),
        ("Acme Corp \u2014 Login", "auth"),
        ("Acme | Suche", "search"),
        ("Acme | Search results", "search"),
        ("Acme | Terms of Use", "legal"),
        ("Acme | AGB ", "legal"),
        ("Impressum | Kontakt", "imprint"),
    ],
)
def test_title_may_end_with_the_type_word(heading: str, expected: str) -> None:
    assert classify(doc(title=heading)) == (expected, ["title"])
    assert classify(doc(body=f"<h1>{heading}</h1>"))[0] == "other"


@pytest.mark.parametrize(
    "heading",
    ["Firma | Impressionen", "Firma Impressum", "Acme-Kontakt", "Firma | Kontakt aufnehmen"],
)
def test_the_type_word_at_the_end_needs_a_separator_before_it(heading: str) -> None:
    assert classify(doc(title=heading))[0] == "other"


def test_url_and_schema_org_still_beat_a_title_suffix() -> None:
    assert classify(doc(title="Firma | Impressum"), "https://example.org/kontakt")[0] == "contact"
    schema = '<script type="application/ld+json">{"@type": "AboutPage"}</script>'
    assert classify(doc(title="Firma | Impressum", head=schema))[0] == "about"


@pytest.mark.parametrize(
    "heading",
    [
        "Firma GmbH Impressum",
        "Kontaktlinsen günstig",
        "Contact lenses | Shop",
        "Login-Seite der Zukunft",
        "Impressionen aus Rom",
        "Suchmaschinen im Vergleich",
        "Über unsere Geschichte",
        "",
    ],
)
def test_headings_that_only_look_similar_do_not_count(heading: str) -> None:
    assert classify(doc(title=heading, body=f"<h1>{heading}</h1>"))[0] == "other"


def test_title_beats_h1_and_only_the_first_h1_counts() -> None:
    html = doc(title="Kontakt", body="<h1>Über uns</h1>")
    assert classify(html) == ("contact", ["title"])
    html = doc(body="<h1>Willkommen</h1><h1>Kontakt</h1>")
    assert classify(html)[0] == "other"


def test_title_beats_password_field() -> None:
    html = doc(title="Anmelden", body='<input type="password">')
    assert classify(html, words=10) == ("auth", ["title", "password_field"])


def test_password_field_on_a_thin_page_is_auth() -> None:
    html = doc(title="Willkommen", body='<form><input type="PASSWORD" name="pw"></form>')
    assert classify(html, words=THIN_PAGE_BELOW_WORDS - 1) == ("auth", ["password_field"])


def test_password_field_on_a_long_page_is_no_auth() -> None:
    html = doc(body='<input type="password">')
    assert classify(html, words=THIN_PAGE_BELOW_WORDS) == ("content", ["word_count"])


def test_password_field_beats_pagination() -> None:
    html = doc(head='<link rel="next" href="/p/2">', body='<input type="password">')
    assert classify(html, words=10)[0] == "auth"


@pytest.mark.parametrize(
    "markup",
    [
        '<link rel="next" href="/p/2">',
        '<link rel="prev" href="/p/1">',
        '<link rel="canonical next" href="/p/2">',
        '<a rel="next" href="/p/2">weiter</a>',
        '<a rel="Prev" href="/p/1">zurück</a>',
    ],
)
def test_rel_next_and_prev_mark_a_listing(markup: str) -> None:
    html = doc(head=markup if markup.startswith("<link") else "", body=markup)
    assert classify(html, words=500) == ("listing", ["pagination"])


def test_pagination_beats_word_count() -> None:
    html = doc(head='<link rel="next" href="/p/2">')
    assert classify(html, words=1000)[0] == "listing"


def link_list(count: int, *, filler_words: int) -> str:
    links = "".join(f'<li><a href="/a/{i}">Beitrag Nummer {i} lesen</a></li>' for i in range(count))
    filler = " ".join(["Text"] * filler_words)
    return doc(body=f"<ul>{links}</ul><p>{filler}</p>")


def test_link_density_marks_a_listing() -> None:
    html = link_list(LISTING_MIN_LINKS, filler_words=5)
    assert classify(html, words=100) == ("listing", ["link_density"])


def test_link_density_needs_enough_links() -> None:
    html = link_list(LISTING_MIN_LINKS - 1, filler_words=0)
    assert classify(html, words=100) == ("content", ["word_count"])


def test_link_density_needs_enough_link_text() -> None:
    html = link_list(LISTING_MIN_LINKS, filler_words=400)
    assert classify(html, words=500) == ("content", ["word_count"])


def test_link_density_ignores_script_style_and_template_text() -> None:
    hidden = "x" * 5000
    links = "".join(f'<a href="/a/{i}">Beitrag Nummer {i}</a> ' for i in range(LISTING_MIN_LINKS))
    html = doc(
        body=(
            f"<script>{hidden}</script><style>{hidden}</style><noscript>{hidden}</noscript>"
            f"<template>{hidden}</template><nav>{links}</nav>"
        )
    )
    assert classify(html, words=100)[0] == "listing"


def test_link_share_constants() -> None:
    assert LISTING_LINK_SHARE == 0.6
    assert LISTING_MIN_LINKS == 20


def test_pagination_and_link_density_are_both_listed() -> None:
    html = link_list(LISTING_MIN_LINKS, filler_words=5).replace(
        "<head>", '<head><link rel="next" href="/2">'
    )
    assert classify(html, words=100) == ("listing", ["pagination", "link_density"])


def test_content_needs_the_minimum_word_count() -> None:
    assert classify(doc(), words=MIN_WORDS) == ("content", ["word_count"])
    assert classify(doc(), words=MIN_WORDS - 1) == ("other", [])


def test_og_type_article_is_a_signal_for_content() -> None:
    html = doc(head='<meta property="og:type" content="article">')
    assert classify(html, words=MIN_WORDS) == ("content", ["og_type", "word_count"])


def test_og_type_alone_does_not_make_content() -> None:
    html = doc(head='<meta property="og:type" content="article">')
    assert classify(html, words=1) == ("other", [])


OG_ARTICLE = '<meta property="og:type" content="article">'


def test_article_overrides_title_listing_and_link_density() -> None:
    titled = doc(head=OG_ARTICLE, title="Kontakt \u2013 So gelingt das Elterngespr\u00e4ch")
    assert classify(titled, "https://example.org/blog/x", words=900) == (
        "content",
        ["og_type", "word_count"],
    )
    headed = doc(head=OG_ARTICLE, body="<h1>Kontakt</h1>")
    assert classify(headed, words=900)[0] == "content"
    paged = doc(head=OG_ARTICLE + '<link rel="next" href="/2">')
    assert classify(paged, "https://example.org/blog/article?page=2", words=900) == (
        "content",
        ["og_type", "word_count"],
    )
    menu = "".join(f'<li><a href="/m/{i}">Men\u00fcpunkt Nummer {i}</a></li>' for i in range(60))
    body = f"<ul>{menu}</ul><p>{' '.join(['Wort'] * 30)}</p>"
    assert classify(doc(body=body), words=220, min_words=50) == ("listing", ["link_density"])
    dense = doc(head=OG_ARTICLE, body=body)
    assert classify(dense, words=220, min_words=50) == ("content", ["og_type", "word_count"])


def test_article_override_keeps_a_url_type() -> None:
    html = doc(head=OG_ARTICLE, title="Impressum")
    assert classify(html, "https://example.org/impressum/", words=900) == (
        "imprint",
        ["url"],
    )


def test_article_override_keeps_schema_org() -> None:
    schema = '<script type="application/ld+json">{"@type": "ContactPage"}</script>'
    assert classify(doc(head=OG_ARTICLE + schema), words=900)[0] == "contact"


def test_article_override_needs_enough_words() -> None:
    html = doc(head=OG_ARTICLE, title="Kontakt")
    assert classify(html, words=10) == ("contact", ["title"])


def test_url_and_schema_org_and_title_all_agree() -> None:
    html = doc(
        head='<script type="application/ld+json">{"@type": "ContactPage"}</script>',
        title="Kontakt",
        body="<h1>Kontakt</h1>",
    )
    assert classify(html, "https://example.org/kontakt") == (
        "contact",
        ["schema_org", "url", "title", "h1"],
    )


def test_garbage_html_does_not_raise() -> None:
    assert classify("")[0] == "other"
    assert classify("<<<>>>&&& \x00 <a href=")[0] == "other"
    assert classify("not html at all", words=MIN_WORDS)[0] == "content"


def test_microdata_scan_is_linear_on_nested_scopes() -> None:
    depth = 5000
    html = doc(
        body='<div itemscope itemtype="https://schema.org/ContactPage">'
        + '<div itemscope itemtype="https://schema.org/WebPage">' * depth
        + "</div>" * depth
        + "</div>"
    )
    started = time.perf_counter()
    result = classify(html)
    assert time.perf_counter() - started < 1.0
    assert result == ("contact", ["schema_org"])


def test_microdata_scan_stops_after_the_scope_limit() -> None:
    scopes = '<div itemscope itemtype="https://schema.org/WebPage"></div>' * 60
    late = '<div itemscope itemtype="https://schema.org/ContactPage"></div>'
    assert classify(doc(body=scopes + late))[0] == "other"
