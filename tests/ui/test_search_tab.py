"""The playground's "Search" tab (package 25, D4 a): shown only to a key that can search
(``features.search``), a form that forwards ``POST /v1/search`` in-process, the hits listed
escaped with a checkbox each, and "Extract selected", which starts a batch of the ticked hits
through the site tree's batch flow (``/ui/run/batch-urls``). SearXNG is a fake on
``httpx.MockTransport`` in ``app.state.search_http``."""

import contextlib
import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any, cast

import httpx
import lxml.html
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from tests.ui.conftest import AUTH, HX, USER_KEY, make_settings
from tests.ui.test_a11y_markup import _described, _ids, _labelled
from tests.ui.test_jobs_views import job_items, jobs_of
from tests.ui.test_operations import UI_CSS
from text_extraction.app import create_public_app
from text_extraction.fetch import Fetcher
from text_extraction.pipeline import create_pipeline
from text_extraction.search.types import MAX_PAGE
from text_extraction.ui.forms import DEFAULT_SEARCH_PAGE, TIME_RANGE_CHOICES, feature_offer
from text_extraction.ui.forward import forward
from text_extraction.ui.i18n import CATALOGUES, LANGUAGE_HEADER, LANGUAGES, Language, translate

ADMIN_KEY = "search-admin-key-0123456789-secret"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
SOURCES = ["wikipedia", "arxiv", "crossref"]
ORIGIN = "https://example.org"
# The client drops tags and decodes entities, so a title that names a tag reaches the template
# as text that must still be escaped.
EVIL_TITLE = "<script>alert(1)</script> Photosynthesis"
EVIL_TITLE_RAW = "&lt;script&gt;alert(1)&lt;/script&gt; Photosynthesis"
CONTROLS = (
    ".//input[not(@type='hidden') and not(@type='submit') and not(@type='button')]"
    " | .//select | .//textarea"
)

Handler = Callable[[httpx.Request], httpx.Response]


def _key(name: str, role: str, raw: str) -> dict[str, Any]:
    return {"name": name, "role": role, "sha256": hashlib.sha256(raw.encode()).hexdigest()}


KEYS = [_key("alice", "user", USER_KEY), _key("root", "admin", ADMIN_KEY)]


class Searx:
    """A fake SearXNG that records the requests it gets."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def answer(
    hits: list[dict[str, Any]], unresponsive: list[list[str]] | None = None
) -> dict[str, Any]:
    return {
        "query": "q",
        "results": hits,
        "suggestions": ["photosynthesis plants"],
        "unresponsive_engines": unresponsive or [],
    }


def hit(url: str, title: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "title": title, "content": "A snippet.", "engines": ["wikipedia"], **extra}


HITS = [
    hit(
        f"{ORIGIN}/photosynthesis",
        EVIL_TITLE_RAW,
        content="Plants <b>make</b> sugar.",
        engines=["wikipedia", "arxiv"],
        publishedDate="2025-03-01T00:00:00",
    ),
    hit(f"{ORIGIN}/chlorophyll", "Chlorophyll"),
    hit(f'{ORIGIN}/q"uote', "Quoted"),
]


def serving(body: dict[str, Any]) -> Searx:
    return Searx(lambda _request: httpx.Response(200, json=body))


@contextlib.contextmanager
def running(searx: Searx | None = None, **overrides: Any) -> Iterator[TestClient]:
    """The public app with the UI and the search on, a user and an admin key, the pipeline
    around one page and ``searx`` as SearXNG."""
    values: dict[str, Any] = {
        "api_keys": KEYS,
        "search_enabled": True,
        "search_sources": SOURCES,
        "search_max_results": 20,
    }
    settings = make_settings(**{**values, **overrides})
    registry = CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    if searx is not None:
        app.state.search_http = searx.client()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def fragment(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def login(client: TestClient, key: dict[str, str] = AUTH, language: Language = "en") -> Any:
    response = client.post("/ui/run/login", headers={**HX, **key, LANGUAGE_HEADER: language})
    assert response.status_code == 200
    return fragment(response.text)


def search(
    client: TestClient,
    key: dict[str, str] = AUTH,
    ui_language: Language = "en",
    **fields: Any,
) -> Any:
    sent = {**HX, **key, LANGUAGE_HEADER: ui_language, "HX-Trigger": "search-form"}
    return client.post("/ui/run/search", data={"query": "photosynthesis", **fields}, headers=sent)


def tab_names(root: Any) -> list[str]:
    return [tab.get("data-tab") for tab in root.xpath("//*[@role='tablist']/*[@role='tab']")]


# --- the tab -------------------------------------------------------------------------------------


def test_tab_is_hidden_while_the_search_is_off() -> None:
    with running(search_enabled=False) as client:
        app = login(client)

    assert "search" not in tab_names(app)
    assert not app.xpath("//form[@id='search-form']")


def test_tab_is_shown_with_features_search() -> None:
    with running() as client:
        app = login(client)

    names = tab_names(app)
    assert names[-2:] == ["search", "info"]
    (tab,) = app.xpath("//*[@role='tab'][@data-tab='search']")
    assert " ".join(tab.text_content().split()) == "Search"
    (form,) = app.xpath("//section[@id='panel-search']//form[@id='search-form']")
    assert form.get("hx-post") == "/ui/run/search"
    assert form.get("hx-target") == "#result"
    (query,) = form.xpath(".//input[@name='query']")
    assert query.get("required") is not None
    assert [box.get("value") for box in form.xpath(".//input[@name='sources']")] == SOURCES
    assert form.xpath(".//input[@name='sources'][@checked]") == []  # none ticked: all sources
    assert form.xpath(".//fieldset[.//input[@name='sources']]/legend")
    (most,) = form.xpath(".//input[@name='max_results']")
    assert (most.get("max"), most.get("value")) == ("20", "10")
    assert [o.get("value") for o in form.xpath(".//select[@name='language']/option")] == [
        "",
        "de",
        "en",
    ]
    assert [o.get("value") for o in form.xpath(".//select[@name='time_range']/option")] == [
        "",
        "day",
        "month",
        "year",
    ]
    assert form.xpath(".//input[@name='page']")


def test_form_presets_come_from_the_settings_and_the_form_constants() -> None:
    # A SEARCH_MAX_RESULTS below the usual preset of 10 lowers the preset, so the form never
    # offers more hits than the API takes; time ranges and result pages follow forms.py and
    # the request model.
    with running(search_max_results=5) as client:
        app = login(client)

    (form,) = app.xpath("//form[@id='search-form']")
    (most,) = form.xpath(".//input[@name='max_results']")
    assert (most.get("max"), most.get("value")) == ("5", "5")
    options = [o.get("value") for o in form.xpath(".//select[@name='time_range']/option")]
    assert tuple(options) == TIME_RANGE_CHOICES
    (page,) = form.xpath(".//input[@name='page']")
    assert (page.get("min"), page.get("max"), page.get("value")) == (
        "1",
        str(MAX_PAGE),
        str(DEFAULT_SEARCH_PAGE),
    )


def test_tab_is_hidden_for_a_user_key_while_search_is_for_admins() -> None:
    with running(search_allow_users=False) as client:
        user = login(client)
        admin = login(client, ADMIN)

    assert "search" not in tab_names(user)
    assert texts(user, "//section[@id='panel-info']//p[contains(@class, 'hint')]") == [
        translate("en", "hint.search_admin_only")
    ]
    assert "search" in tab_names(admin)
    assert not admin.xpath("//section[@id='panel-info']//p[contains(@class, 'hint')]")


def test_feature_offer_has_search_and_its_admin_only_mark() -> None:
    settings = make_settings(api_keys=KEYS, search_enabled=True, search_allow_users=False)

    user, admin = feature_offer("user", settings), feature_offer("admin", settings)

    assert (user.search, "search" in user.admin_only) == (False, True)
    assert (admin.search, admin.admin_only) == (True, frozenset())
    off = feature_offer("user", make_settings(search_enabled=False))
    assert (off.search, "search" in off.admin_only) == (False, False)


# --- the search ----------------------------------------------------------------------------------


def test_hits_render_escaped_with_safe_links() -> None:
    with running(serving(answer(HITS))) as client:
        response = search(client)

    assert response.status_code == 200
    assert "<script>alert" not in response.text
    root = fragment(response.text)
    items = root.xpath("//ol[@class='search-hits']/li")
    assert len(items) == 3
    first = items[0]
    (link,) = first.xpath(".//a[contains(@class, 'hit-link')]")
    assert link.get("href") == f"{ORIGIN}/photosynthesis"
    assert link.get("rel") == "noopener noreferrer"
    assert link.get("target") == "_blank"
    assert EVIL_TITLE in link.text_content()
    (box,) = first.xpath(".//input[@type='checkbox']")
    assert (box.get("name"), box.get("value")) == ("url", f"{ORIGIN}/photosynthesis")
    assert box.get("checked") is None
    assert texts(first, ".//*[contains(@class, 'hit-url')]") == [f"{ORIGIN}/photosynthesis"]
    assert texts(first, ".//*[contains(@class, 'hit-snippet')]") == ["Plants make sugar."]
    assert "wikipedia, arxiv" in " ".join(texts(first, ".//*[contains(@class, 'hit-meta')]"))
    assert "2025-03-01" in " ".join(texts(first, ".//*[contains(@class, 'hit-meta')]"))
    assert items[2].xpath(".//input[@type='checkbox']")[0].get("value") == f'{ORIGIN}/q"uote'
    # The status line and the announcement say the count.
    assert texts(root, "//header[@class='answer']/span[contains(@class, 'badge')]") == [
        translate("en", "status.success")
    ]
    (view,) = root.xpath("//article[@class='result-view']")
    assert view.get("data-announce") == "Result: success, 3 hits"


def test_no_ticked_source_sends_no_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    async def recording(request: Any, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/v1/search":
            bodies.append(kwargs["json"])
        return await forward(request, method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("text_extraction.ui.routes.forward", recording)
    searx = serving(answer(HITS))
    with running(searx) as client:
        search(client)
        search(client, sources=["wikipedia", "arxiv"], language="de", time_range="year", page="2")

    assert "sources" not in bodies[0]
    assert bodies[0]["query"] == "photosynthesis"
    assert searx.requests[0].url.params["engines"] == "wikipedia,arxiv,crossref"
    assert bodies[1]["sources"] == ["wikipedia", "arxiv"]
    assert (bodies[1]["language"], bodies[1]["time_range"], bodies[1]["page"]) == ("de", "year", 2)
    assert searx.requests[1].url.params["engines"] == "wikipedia,arxiv"


def test_a_malformed_number_is_a_form_error_on_its_field() -> None:
    searx = serving(answer(HITS))
    with running(searx) as client:
        response = search(client, max_results="many")

    (error,) = fragment(response.text).xpath("//div[@role='alert']")
    assert error.get("data-error-for") == "search-max-results"
    assert searx.requests == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_language_is_preset_to_the_ui_language(language: Language) -> None:
    """German results by default for a German playground: Wikipedia and its sisters search in
    the chosen language."""
    with running() as client:
        app = login(client, language=language)

    (form,) = app.xpath("//form[@id='search-form']")
    assert [o.get("value") for o in form.xpath(".//select[@name='language']/option[@selected]")] == [
        language
    ]


def test_all_languages_sends_no_language() -> None:
    searx = serving(answer(HITS))
    with running(searx) as client:
        search(client, language="")

    assert "language" not in searx.requests[0].url.params


def test_a_language_outside_the_list_is_refused_before_searxng() -> None:
    searx = serving(answer(HITS))
    with running(searx) as client:
        response = search(client, language="German")

    root = fragment(response.text)
    assert root.xpath("//div[@role='alert'][@data-error-for='search-language']")
    assert "German" in " ".join(texts(root, "//div[@role='alert']/p"))
    assert searx.requests == []


def test_a_failed_search_shows_its_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with running(Searx(down)) as client:
        response = search(client)

    root = fragment(response.text)
    assert texts(root, "//header[@class='answer']/span[contains(@class, 'badge')]") == [
        translate("en", "status.failed")
    ]
    (error,) = root.xpath("//div[@class='error'][@role='alert']")
    assert texts(error, ".//span[@class='error-code']") == ["search_failed"]
    assert texts(error, ".//span[@class='error-text']") == [translate("en", "error.search_failed")]
    assert error.xpath(".//span[@class='retryable']")
    assert not root.xpath("//form[@id='search-hits-form']")


def test_a_partial_search_lists_the_failed_sources() -> None:
    body = answer(HITS[1:2], unresponsive=[["arxiv", "timeout"]])
    with running(serving(body)) as client:
        response = search(client)

    root = fragment(response.text)
    assert texts(root, "//header[@class='answer']/span[contains(@class, 'badge')]") == [
        translate("en", "status.partial")
    ]
    (notice,) = root.xpath("//*[contains(@class, 'search-sources-failed')]")
    assert notice.get("role") == "note"
    assert translate("en", "search.sources_failed") in " ".join(texts(notice, "."))
    assert texts(notice, ".//li") == ["arxiv: timeout"]
    assert len(root.xpath("//ol[@class='search-hits']/li")) == 1


def test_a_failed_search_lists_the_sources_that_did_not_answer() -> None:
    body = answer([], unresponsive=[[name, "timeout"] for name in SOURCES])
    with running(serving(body)) as client:
        root = fragment(search(client).text)

    (error,) = root.xpath("//div[@class='error'][@role='alert']")
    assert texts(error, ".//span[@class='error-code']") == ["search_failed"]
    assert texts(error, ".//ul[@class='details']/li") == [f"{name}: timeout" for name in SOURCES]


def test_no_hits_with_a_failed_source_is_partial_and_names_it() -> None:
    with running(serving(answer([], unresponsive=[["arxiv", "timeout"]]))) as client:
        root = fragment(search(client).text)

    assert texts(root, "//header[@class='answer']/span[contains(@class, 'badge')]") == [
        translate("en", "status.partial")
    ]
    assert texts(root, "//p[@class='search-empty']") == [translate("en", "search.no_hits")]
    (notice,) = root.xpath("//*[contains(@class, 'search-sources-failed')]")
    assert texts(notice, ".//li") == ["arxiv: timeout"]


def test_no_hits_says_so() -> None:
    with running(serving(answer([]))) as client:
        root = fragment(search(client).text)

    assert texts(root, "//p[@class='search-empty']") == [translate("en", "search.no_hits")]
    assert not root.xpath("//form[@id='search-hits-form']")


def test_search_refused_for_a_user_key_while_for_admins() -> None:
    searx = serving(answer(HITS))
    with running(searx, search_allow_users=False) as client:
        root = fragment(search(client).text)

    assert texts(root, "//*[@class='http-status']") == ["HTTP 403"]
    assert searx.requests == []


# --- extract selected ----------------------------------------------------------------------------


def _hits_form(client: TestClient) -> Any:
    (form,) = fragment(search(client).text).xpath("//form[@id='search-hits-form']")
    return form


def test_extract_selected_starts_a_batch_of_the_ticked_hits() -> None:
    with running(serving(answer(HITS))) as client:
        form = _hits_form(client)
        assert form.get("hx-post") == "/ui/run/batch-urls"
        assert form.get("hx-target") == "next .search-jobs"
        assert form.getnext() is not None
        assert "search-jobs" in (form.getnext().get("class") or "")
        assert {field.get("name") for field in form.xpath(".//input | .//textarea")} >= {
            "url",
            "format",
            "metadata",
            "options",
        }
        assert texts(form, ".//button[@type='submit']") == ["Extract selected"]

        selected = [f"{ORIGIN}/photosynthesis", f'{ORIGIN}/q"uote']
        response = client.post(
            "/ui/run/batch-urls",
            data={"url": selected, "format": "markdown", "mode": "auto", "metadata": "on"},
            headers={**HX, **AUTH, "HX-Trigger": "search-hits-form"},
        )

        (job_id,) = jobs_of(response.text)
        assert job_items(client, job_id) == selected


def test_extract_selected_with_nothing_ticked_is_a_form_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: list[Any] = []

    async def counting(request: Any, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/v1/batch":
            batches.append(kwargs["json"])
        return await forward(request, method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("text_extraction.ui.job_views.forward", counting)
    with running(serving(answer(HITS))) as client:
        response = client.post(
            "/ui/run/batch-urls",
            data={"format": "markdown", "mode": "auto", "metadata": "on"},
            headers={**HX, **AUTH, "HX-Trigger": "search-hits-form"},
        )

    (error,) = fragment(response.text).xpath("//div[@role='alert']")
    assert " ".join(error.text_content().split()) == translate("en", "jobs.no_urls")
    assert batches == []


# --- languages and accessibility -----------------------------------------------------------------

NEW_KEYS = (
    "tab.search",
    "help.search.intro",
    "form.search_query",
    "form.search_sources",
    "form.search_language",
    "form.search_language.any",
    "form.search_language.de",
    "form.search_language.en",
    "form.search_max_results",
    "form.search_time_range",
    "form.search_time_range.any",
    "form.search_time_range.day",
    "form.search_time_range.month",
    "form.search_time_range.year",
    "form.search_page",
    "form.search_submit",
    "hint.search_query",
    "hint.search_sources",
    "hint.search_language",
    "hint.search_max_results",
    "hint.search_time_range",
    "hint.search_page",
    "hint.search_admin_only",
    "search.hits_title",
    "search.hits_label",
    "search.hits.one",
    "search.hits.other",
    "search.sources_failed",
    "search.suggestions",
    "search.no_hits",
    "search.found_by",
    "search.published",
    "search.select_all",
    "search.select_none",
    "status.search_result",
)


def test_new_keys_are_in_both_catalogues() -> None:
    for language in LANGUAGES:
        missing = [key for key in NEW_KEYS if key not in CATALOGUES[language]]
        assert missing == [], language
    assert CATALOGUES["de"]["tab.search"] != CATALOGUES["en"]["tab.search"]


@pytest.mark.parametrize("language", LANGUAGES)
def test_tab_and_hits_speak_the_language(language: Language) -> None:
    with running(serving(answer(HITS))) as client:
        app = login(client, language=language)
        root = fragment(search(client, ui_language=language).text)

    (tab,) = app.xpath("//*[@role='tab'][@data-tab='search']")
    assert " ".join(tab.text_content().split()) == translate(language, "tab.search")
    assert texts(app, "//section[@id='panel-search']//button[@type='submit']") == [
        translate(language, "form.search_submit")
    ]
    assert texts(root, "//form[@id='search-hits-form']//button[@type='submit']") == [
        translate(language, "tree.extract_selected")
    ]
    heading = translate(language, "search.hits_title")
    assert heading in texts(root, "//section[contains(@class, 'search-results')]/*[1]")


@pytest.mark.parametrize("language", LANGUAGES)
def test_tab_and_hits_are_labelled_and_described(language: Language) -> None:
    body = answer(HITS, unresponsive=[["arxiv", "timeout"]])
    with running(serving(body), auth_mode="none") as client:
        page = lxml.html.document_fromstring(client.get("/ui", params={"lang": language}).text)
        root = fragment(search(client, key={}, ui_language=language).text)

    (panel,) = page.xpath("//section[@id='panel-search']")
    controls = panel.xpath(CONTROLS)
    assert controls
    assert [c.get("name") for c in controls if not _labelled(c, page)] == []
    assert [c.get("name") for c in controls if not _described(c, page)] == []
    hits_controls = root.xpath(CONTROLS)
    assert hits_controls
    assert [c.get("name") for c in hits_controls if not _labelled(c, root)] == []
    # Every hit's box is named by its title.
    for box in root.xpath("//ol[@class='search-hits']//input[@type='checkbox']"):
        (title,) = root.xpath(f"//*[@id='{box.get('aria-labelledby')}']")
        assert title.text_content().strip()
    page_ids, fragment_ids = _ids(page), _ids(root)
    assert len(page_ids) == len(set(page_ids))
    assert len(fragment_ids) == len(set(fragment_ids))
    assert set(page_ids) & set(fragment_ids) == set()
    known = set(page_ids) | set(fragment_ids)
    for element in root.xpath("//*[@aria-describedby or @aria-labelledby]"):
        for attribute in ("aria-describedby", "aria-labelledby"):
            assert set((element.get(attribute) or "").split()) <= known


def test_search_operation_is_listed() -> None:
    from text_extraction.ui.forms import OPERATIONS

    assert (OPERATIONS["search"].method, OPERATIONS["search"].path) == ("POST", "/v1/search")
    assert OPERATIONS["search"].form == "forms/search.html"


def test_json_of_the_answer_is_shown() -> None:
    with running(serving(answer(HITS))) as client:
        root = fragment(search(client).text)

    (raw,) = root.xpath("//details[@class='raw']/pre")
    assert json.loads(raw.text_content())["data"]["results"][0]["title"] == EVIL_TITLE
    (curl,) = root.xpath("//pre[@class='curl']")
    assert "/v1/search" in curl.text_content()


def test_hits_card_is_padded_and_long_snippets_are_clamped() -> None:
    """The hits card has the result cards' padding, and a snippet shows at most four lines."""
    css = UI_CSS.read_text(encoding="utf-8")
    card = css.split(".card.search-results {", 1)[1].split("}", 1)[0]
    assert "padding: 20px 24px;" in card
    snippet = css.split(".hit-snippet {", 1)[1].split("}", 1)[0]
    assert "line-clamp: 4;" in snippet
