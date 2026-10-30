"""Package 8's done-when test: every strategy of ``structured`` and every chunking strategy, end to
end through the API, on one German teaching page (``pages/brueche.html``, written for the purpose:
JSON-LD, microdata, a table, headings, lists and enough text for each chunker).

It runs in the default suite and needs no real LLM: the LLM endpoint is pytest-httpserver
(OpenAI-compatible ``/chat/completions`` and ``/embeddings``), and the llm worker (and, for the
batch, an extract worker) is the real ``Worker`` with its real runner, in a thread of its own on
the fakeredis server of the app, as in ``tests/test_llm_dispatch.py``.

Covered: ``metadata``; ``selectors`` (CSS for a user key, XPath with an admin key, XPath refused
for a user key); ``patterns``; ``llm``; ``auto`` (the LLM request carries only the fields still
missing); ``tables`` (of the page, and of a Markdown text); ``learn_selectors``, then reuse (the
second request makes no LLM call and reports ``sources: learned``; another key learned nothing);
each chunking strategy (``fixed``, ``recursive``, ``markdown``, ``sentence``, ``semantic``) on the
page; ``POST /v1/structured``, ``POST /v1/chunk`` with a ``url`` and with a ``text``; and a batch
with ``structured`` and ``chunking`` through the extract worker and the llm worker.
"""

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.fakes import fetched
from tests.structured.post_helpers import CHAT, EMBEDDINGS, answer, chat_requests, serve_embeddings
from tests.test_llm_dispatch import CALLER_KEY, LlmService, keys_sent
from tests.test_pii_dispatch import USER_KEY
from text_extraction.conversion_pool import ConversionPool
from text_extraction.fetch import Fetcher
from text_extraction.jobs import JobStore
from text_extraction.llm_dispatch import create_llm_move, llm_queue
from text_extraction.llm_worker import LlmRunner
from text_extraction.pipeline import create_pipeline
from text_extraction.scheduler import FairQueue
from text_extraction.structured.prompts import CALLER_NOTES, LEARN_SYSTEM
from text_extraction.structured.store import SelectorStore
from text_extraction.worker import ExtractRunner, Worker

PAGE_URL = "https://lernen.example.org/mathe/brueche.html"
PAGE_HTML = (Path(__file__).parent / "pages" / "brueche.html").read_text(encoding="utf-8")
TITLE = "Brüche addieren und subtrahieren"
MERKSATZ = (
    "Brüche mit gleichem Nenner werden addiert, indem man die Zähler addiert und den Nenner "
    "beibehält."
)

ADMIN_KEY = "admin-key-0123456789-raw-secret"
USER = {"Authorization": f"Bearer {USER_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
USER_LLM = {**USER, "X-LLM-Api-Key": CALLER_KEY}
ADMIN_LLM = {**ADMIN, "X-LLM-Api-Key": CALLER_KEY}
API_KEYS = [
    {"name": "alice", "role": "user", "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest()},
    {"name": "root", "role": "admin", "sha256": hashlib.sha256(ADMIN_KEY.encode()).hexdigest()},
]

STRING: dict[str, Any] = {"type": "string"}
INTEGER: dict[str, Any] = {"type": "integer"}
STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def schema(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


# What the fake LLM answers: the values of an extraction (each field the request asks for) and
# the selectors of a learning call.
LLM_VALUES: dict[str, Any] = {"merksatz": MERKSATZ, "schwierigkeit": "leicht"}
LEARNED_CSS: dict[str, Any] = {"merksatz": {"css": "p.merksatz", "attr": None}}


def topic(text: str) -> list[float]:
    """The fake embedding: subtraction and addition are two directions."""
    return [0.0, 1.0] if "subtrah" in text.lower() or "abgezogen" in text else [1.0, 0.0]


def serve_llm(endpoint: HTTPServer) -> None:
    """The OpenAI-compatible endpoint: chat answers by the request's own strict schema, so that
    it fits whatever fields a request asks for; embeddings by ``topic``."""

    def chat(request: Request) -> Response:
        body = request.get_json()
        fields = list(body["response_format"]["json_schema"]["schema"]["properties"])
        known = LEARNED_CSS if body["messages"][0]["content"] == LEARN_SYSTEM else LLM_VALUES
        reply = answer({name: known.get(name) for name in fields})
        return Response(json.dumps(reply), content_type="application/json")

    endpoint.expect_request(CHAT, method="POST").respond_with_handler(chat)
    serve_embeddings(endpoint, topic)


def embedding_requests(endpoint: HTTPServer) -> list[dict[str, Any]]:
    return [request.get_json() for request, _ in endpoint.log if request.path == EMBEDDINGS]


def asked_fields(request: dict[str, Any]) -> list[str]:
    """The fields an extraction request asks the LLM for (its strict schema's properties)."""
    format_ = request["response_format"]
    assert format_["type"] == "json_schema"
    assert format_["json_schema"]["strict"] is True
    return list(format_["json_schema"]["schema"]["properties"])


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


class Workers:
    """The real llm ``Worker`` with ``LlmRunner`` and, with ``extract``, the real extract
    ``Worker`` with ``ExtractRunner`` on the service's fake fetcher (its results that need the
    LLM move on to ``llm``); on a loop of their own, sharing the fakeredis server of the app."""

    def __init__(self, service: LlmService, *, extract: bool = False) -> None:
        self._service = service
        self._extract = extract
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        settings = self._service.settings
        valkey = FakeAsyncRedis(server=self._service.server)
        store = JobStore(valkey, settings)
        registry = CollectorRegistry()
        selectors = SelectorStore(valkey, settings.selector_learn_ttl_seconds)
        workers = [
            Worker(
                settings,
                valkey,
                LlmRunner(settings, store, selectors, registry=registry),
                store,
                llm_queue(valkey, settings),
                registry,
                concurrency=1,
                sleep=_short_sleep,
            )
        ]
        pool = ConversionPool(1)
        if self._extract:
            pipeline = create_pipeline(
                settings, valkey, cast(Fetcher, self._service.fetcher), registry, pool
            )
            workers.append(
                Worker(
                    settings,
                    valkey,
                    ExtractRunner(pipeline, store, settings, None, registry=registry),
                    store,
                    FairQueue(valkey, settings),
                    registry,
                    concurrency=1,
                    sleep=_short_sleep,
                    llm_move=create_llm_move(settings, valkey, store, None, registry),
                )
            )
        self._ready.set()
        try:
            async with asyncio.TaskGroup() as running:
                for worker in workers:
                    running.create_task(worker.run(self._stop))
        finally:
            pool.shutdown()

    def __enter__(self) -> "Workers":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


@pytest.fixture
def service(httpserver: HTTPServer) -> LlmService:
    serve_llm(httpserver)
    return LlmService(
        httpserver,
        {PAGE_URL: fetched(PAGE_URL, PAGE_HTML)},
        api_keys=API_KEYS,
        llm_embedding_model="embed-1",
    )


def structured(
    client: TestClient, options: dict[str, Any], headers: dict[str, str] = USER, **body: Any
) -> dict[str, Any]:
    """``POST /v1/structured`` of the page: a 200 with ``status: success``, its body."""
    response = client.post(
        "/v1/structured", json={"url": PAGE_URL, "structured": options, **body}, headers=headers
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "success", result["error"]
    assert CALLER_KEY not in response.text
    assert result["data"]["structured"]["stage"] == "done"
    assert result["data"]["structured"]["learn_html"] is None
    return cast(dict[str, Any], result)


def chunk(client: TestClient, headers: dict[str, str] = USER, **body: Any) -> dict[str, Any]:
    """``POST /v1/chunk``: a 200 with ``status: success``, its body."""
    response = client.post("/v1/chunk", json=body, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "success", result["error"]
    return cast(dict[str, Any], result)


def assert_chunks(content: str, chunks: list[dict[str, Any]], *, size: int, overlap: int) -> None:
    """The invariants of S10: each chunk is its slice of ``content``, in order, at most ``size``
    characters, neighbours share at most ``overlap``, and only whitespace is left out."""
    assert chunks
    for index, item in enumerate(chunks):
        assert item["index"] == index
        assert content[item["start"] : item["end"]] == item["text"]
        assert 0 < len(item["text"]) <= size
        if index:
            previous = chunks[index - 1]
            assert item["start"] > previous["start"]
            assert previous["end"] - item["start"] <= overlap
            assert content[previous["end"] : item["start"]].strip() == ""
    assert content[: chunks[0]["start"]].strip() == ""
    assert content[chunks[-1]["end"] :].strip() == ""


# --- structured: the strategies ---


def test_metadata_from_json_ld_microdata_and_meta(
    service: LlmService, httpserver: HTTPServer
) -> None:
    fields = schema(
        name=STRING,
        educational_level=STRING,
        author=STRING,
        date_published=STRING,
        time_required=STRING,  # microdata (itemprop), not in the JSON-LD
        keywords=STRINGS,  # <meta name="keywords">, a comma list
        site_name=STRING,  # OpenGraph og:site_name
        unknown_field=STRING,
    )
    with TestClient(service.app) as client:
        body = structured(client, {"schema": fields, "strategy": "metadata"})

    result = body["data"]["structured"]
    assert result["values"] == {
        "name": TITLE,
        "educational_level": "Klasse 6",
        "author": "Redaktion Mathewerkstatt",
        "date_published": "2026-09-14",
        "time_required": "PT45M",
        "keywords": ["Bruchrechnung", "Brüche", "Hauptnenner", "Klasse 6"],
        "site_name": "Mathewerkstatt",
        "unknown_field": None,
    }
    assert set(result["sources"].values()) == {"metadata"}
    assert result["missing"] == ["unknown_field"]
    assert result["llm"] is None
    assert body["data"]["content"].startswith(f"# {TITLE}")  # the extraction is unchanged
    assert httpserver.log == []  # no LLM call


def test_selectors_css_for_a_user_key_and_xpath_with_an_admin_key(
    service: LlmService, httpserver: HTTPServer
) -> None:
    fields = schema(lernziele=STRINGS, klassenstufe=STRING, kontakt=STRING, aufgaben=STRINGS)
    css = {
        "schema": fields,
        "strategy": "selectors",
        "selectors": {
            "lernziele": {"css": "ul.lernziele li"},
            "klassenstufe": {"css": "p.steckbrief span.klasse"},
            "kontakt": {"css": "nav a[href*=kontakt]", "attr": "href"},
        },
    }
    xpath = {
        "schema": fields,
        "strategy": "selectors",
        "selectors": {
            "aufgaben": {"xpath": "//ol[@class='aufgaben']/li"},
            "klassenstufe": {"xpath": "string(//span[@class='klasse'])"},
        },
    }
    with TestClient(service.app) as client:
        by_css = structured(client, css)
        by_xpath = structured(client, xpath, ADMIN)
        refused = client.post(
            "/v1/structured", json={"url": PAGE_URL, "structured": xpath}, headers=USER
        )

    values = by_css["data"]["structured"]["values"]
    assert values == {
        "lernziele": [
            "Ich kann gleichnamige Brüche addieren und subtrahieren.",
            "Ich kann den Hauptnenner zweier Brüche bestimmen.",
            "Ich kann ungleichnamige Brüche addieren und subtrahieren.",
        ],
        "klassenstufe": "Klassenstufe 6",
        "kontakt": "https://lernen.example.org/kontakt/",  # made absolute
        "aufgaben": None,
    }
    assert by_css["data"]["structured"]["sources"] == {
        "lernziele": "selectors",
        "klassenstufe": "selectors",
        "kontakt": "selectors",
    }
    admin = by_xpath["data"]["structured"]
    assert admin["values"]["klassenstufe"] == "Klassenstufe 6"
    assert admin["values"]["aufgaben"] == [
        "Berechne zwei Neuntel plus fünf Neuntel und kürze, wenn es geht.",
        "Bestimme den Hauptnenner von drei Achteln und fünf Zwölfteln.",
        "Subtrahiere ein Drittel von drei Vierteln und erkläre jeden Schritt.",
    ]
    assert admin["missing"] == ["lernziele", "kontakt"]
    assert refused.status_code == 422  # XPath needs an admin key (R4)
    assert refused.json()["error"]["message"] == "XPath selectors need an admin key; use CSS."
    assert httpserver.log == []


def test_patterns_on_the_content(service: LlmService, httpserver: HTTPServer) -> None:
    options = {
        "schema": schema(bearbeitungszeit=INTEGER, klassenstufe=INTEGER, hauptnenner=STRINGS),
        "strategy": "patterns",
        "patterns": {
            "bearbeitungszeit": {"regex": r"Bearbeitungszeit: (\d+) Minuten", "group": 1},
            "klassenstufe": {
                "regex": r"klassenstufe (?P<stufe>\d+)",
                "group": "stufe",
                "flags": ["i"],
            },
            "hauptnenner": {"regex": r"\| (\d+) \|", "group": 1},
        },
    }
    with TestClient(service.app) as client:
        body = structured(client, options)

    result = body["data"]["structured"]
    assert result["values"] == {
        "bearbeitungszeit": 45,
        "klassenstufe": 6,
        "hauptnenner": ["6", "12", "10"],  # an array field takes every match
    }
    assert set(result["sources"].values()) == {"patterns"}
    assert httpserver.log == []


def test_llm_reads_the_content_through_the_llm_worker(
    service: LlmService, httpserver: HTTPServer
) -> None:
    options = {
        "schema": schema(
            merksatz=STRING,
            schwierigkeit={"type": "string", "enum": ["leicht", "mittel", "schwer"]},
        ),
        "strategy": "llm",
        "instructions": "Schwierigkeit: aus Sicht der Klasse 6.",
    }
    with TestClient(service.app) as client, Workers(service):
        body = structured(client, options, USER_LLM)
        assert service.wait_for_no_jobs() == []  # the llm job is gone with the answer

    result = body["data"]["structured"]
    assert result["values"] == {"merksatz": MERKSATZ, "schwierigkeit": "leicht"}
    assert result["sources"] == {"merksatz": "llm", "schwierigkeit": "llm"}
    assert result["llm"]["calls"] == 1
    assert result["llm"]["prompt_tokens"] == 100
    assert body["data"]["timings"]["llm_ms"] >= 0
    [request] = chat_requests(httpserver)
    assert asked_fields(request) == ["merksatz", "schwierigkeit"]
    system, user = request["messages"][0]["content"], request["messages"][1]["content"]
    assert system.endswith(f"{CALLER_NOTES}\nSchwierigkeit: aus Sicht der Klasse 6.")
    assert user.startswith("<document>\n# Brüche addieren und subtrahieren")  # the page as data
    assert "Hauptnenner" in user
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]  # the caller's key wins
    assert service.llm_outcomes() == {"answered": 1}


def test_auto_asks_the_llm_only_for_the_missing_fields(
    service: LlmService, httpserver: HTTPServer
) -> None:
    options = {
        "schema": schema(
            name=STRING,
            lernziele=STRINGS,
            bearbeitungszeit=INTEGER,
            merksatz=STRING,
        ),
        "selectors": {"lernziele": {"css": "ul.lernziele li"}},
        "patterns": {"bearbeitungszeit": {"regex": r"Bearbeitungszeit: (\d+)", "group": 1}},
    }  # strategy: auto, the default
    with TestClient(service.app) as client, Workers(service):
        body = structured(client, options, USER_LLM)

    result = body["data"]["structured"]
    assert result["sources"] == {
        "name": "metadata",
        "lernziele": "selectors",
        "bearbeitungszeit": "patterns",
        "merksatz": "llm",
    }
    assert result["values"]["name"] == TITLE
    assert result["values"]["bearbeitungszeit"] == 45
    assert result["values"]["merksatz"] == MERKSATZ
    assert len(result["values"]["lernziele"]) == 3
    assert result["missing"] == []
    [request] = chat_requests(httpserver)
    assert asked_fields(request) == ["merksatz"]  # only what the cheaper strategies missed


def test_tables_of_the_page_and_of_a_markdown_text(
    service: LlmService, httpserver: HTTPServer
) -> None:
    text = (
        "# Brüche im Alltag\n\n| Lebensmittel | Anteil |\n| --- | --- |\n| Pizza | 3/8 |\n"
        "| Kuchen | 1/4 |\n\nEin Achtel ist die Hälfte eines Viertels.\n"
    )
    with TestClient(service.app) as client:
        page = structured(client, {"tables": True})  # no schema: tables only
        markdown = chunk(
            client, text=text, chunking={"strategy": "fixed"}, structured={"tables": True}
        )

    assert page["data"]["structured"]["values"] == {}
    assert page["data"]["structured"]["tables"] == [
        {
            "index": 0,
            "caption": "Beispiele zum Üben",
            "headers": ["Aufgabe", "Hauptnenner", "Ergebnis"],
            "rows": [
                ["1/2 + 1/3", "6", "5/6"],
                ["3/4 - 1/6", "12", "7/12"],
                ["2/5 + 3/10", "10", "7/10"],
            ],
        }
    ]
    assert markdown["data"]["mode_used"] == "upload"
    [table] = markdown["data"]["structured"]["tables"]
    assert table["headers"] == ["Lebensmittel", "Anteil"]
    assert table["rows"] == [["Pizza", "3/8"], ["Kuchen", "1/4"]]
    assert httpserver.log == []


def test_learn_selectors_then_reuse_without_the_llm(
    service: LlmService, httpserver: HTTPServer
) -> None:
    fields = schema(merksatz=STRING)
    with TestClient(service.app) as client, Workers(service):
        learning = structured(
            client, {"schema": fields, "strategy": "llm", "learn_selectors": True}, USER_LLM
        )
        calls_after_learning = len(chat_requests(httpserver))
        reuse = structured(client, {"schema": fields, "strategy": "auto"}, USER_LLM)
        calls_after_reuse = len(chat_requests(httpserver))
        other_key = structured(client, {"schema": fields, "strategy": "auto"}, ADMIN_LLM)

    learned = learning["data"]["structured"]
    assert learned["values"] == {"merksatz": MERKSATZ}
    assert learned["sources"] == {"merksatz": "llm"}
    assert learned["learned"] == {"stored": ["merksatz"], "rejected": []}
    assert calls_after_learning == 2  # the extraction and the learning call
    learn_request = chat_requests(httpserver)[1]
    assert learn_request["messages"][0]["content"] == LEARN_SYSTEM

    again = reuse["data"]["structured"]
    assert again["values"] == {"merksatz": MERKSATZ}
    assert again["sources"] == {"merksatz": "learned"}
    assert again["llm"] is None
    assert calls_after_reuse == calls_after_learning  # no LLM call for the reuse
    assert service.stored("sel:{alice}:lernen.example.org:*") != []

    # Learned selectors belong to the key that learned them (D2 a): another key asks the LLM.
    assert other_key["data"]["structured"]["sources"] == {"merksatz": "llm"}
    assert len(chat_requests(httpserver)) == calls_after_reuse + 1


# --- chunking ---

CHUNK_SIZE = 300
CHUNK_OVERLAP = 60


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "sentence"])
def test_each_chunker_on_the_page(
    strategy: str, service: LlmService, httpserver: HTTPServer
) -> None:
    options = {"strategy": strategy, "size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP}
    with TestClient(service.app) as client:
        body = chunk(client, url=PAGE_URL, chunking=options)

    data = body["data"]
    content, chunks = data["content"], data["chunks"]
    assert data["mode_used"] == "fast"
    assert_chunks(content, chunks, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    assert len(chunks) > 3
    assert chunks[0]["headings"] == [TITLE]
    assert httpserver.log == []
    texts = [item["text"] for item in chunks]
    if strategy == "fixed":
        assert all(len(text) >= CHUNK_SIZE - CHUNK_OVERLAP for text in texts[:-1])
    if strategy == "markdown":
        assert any(
            item["headings"] == [TITLE, "Ungleichnamige Brüche", "Den Hauptnenner finden"]
            for item in chunks
        )
        for text in texts:  # a heading never ends a chunk
            assert not text.rstrip().splitlines()[-1].startswith("#")
    if strategy == "sentence":
        for text in texts:  # whole sentences; "z. B." and "bzw." end none
            assert text.endswith((".", "!", "?"))
            assert not text.endswith(("z.", "z. B.", "bzw."))
        assert any("z. B. sechs Achtel" in text for text in texts)


def test_semantic_chunks_through_the_llm_worker(
    service: LlmService, httpserver: HTTPServer
) -> None:
    options = {"strategy": "semantic", "size": 600, "overlap": 0}
    with TestClient(service.app) as client, Workers(service):
        body = chunk(client, USER_LLM, url=PAGE_URL, chunking=options)

    data = body["data"]
    assert_chunks(data["content"], data["chunks"], size=600, overlap=0)
    assert data["warnings"] == []
    sent = [text for request in embedding_requests(httpserver) for text in request["input"]]
    assert any("Hauptnenner" in text for text in sent)
    assert all(request["model"] == "embed-1" for request in embedding_requests(httpserver))
    assert chat_requests(httpserver) == []
    assert service.llm_outcomes() == {"answered": 1}


# --- POST /v1/chunk with a text ---

ADDITION = " ".join(
    f"Beim Addieren von {name} Brüchen werden die Zähler zusammengezählt."
    for name in ("zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn", "elf")
)
SUBTRACTION = " ".join(
    f"Beim Subtrahieren von {name} Brüchen werden die Zähler abgezogen."
    for name in ("zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn", "elf")
)
LESSON = (
    "# Bruchrechnung\n\nBrüche beschreiben Anteile eines Ganzen, z. B. ein Viertel einer Pizza.\n\n"
    f"## Addieren\n\n{ADDITION}\n\n### Ungleichnamige Brüche\n\nZuerst sucht man den Hauptnenner. "
    "Dann erweitert man beide Brüche.\n\n## Subtrahieren\n\nMan zieht die Zähler voneinander ab.\n"
)


def test_chunk_text_markdown_keeps_the_heading_path(
    service: LlmService, httpserver: HTTPServer
) -> None:
    options = {"strategy": "markdown", "size": 1000, "overlap": 0}
    with TestClient(service.app) as client:
        body = chunk(client, text=LESSON, chunking=options)

    data = body["data"]
    assert (data["mode_used"], data["url"]) == ("upload", None)
    assert_chunks(data["content"], data["chunks"], size=1000, overlap=0)
    assert [item["headings"] for item in data["chunks"]] == [
        ["Bruchrechnung"],
        ["Bruchrechnung", "Addieren"],
        ["Bruchrechnung", "Addieren", "Ungleichnamige Brüche"],
        ["Bruchrechnung", "Subtrahieren"],
    ]
    assert data["chunks"][1]["text"].startswith("## Addieren")
    assert httpserver.log == []


def test_chunk_text_semantic_cuts_where_the_topic_changes(
    service: LlmService, httpserver: HTTPServer
) -> None:
    with TestClient(service.app) as client, Workers(service):
        body = chunk(
            client,
            USER_LLM,
            text=f"{ADDITION} {SUBTRACTION}",
            text_type="plain",
            chunking={"strategy": "semantic", "size": 2000, "overlap": 0},
        )

    data = body["data"]
    assert data["format"] == "text"
    assert [item["text"] for item in data["chunks"]] == [ADDITION, SUBTRACTION]
    assert all(item["headings"] == [] for item in data["chunks"])
    [request] = embedding_requests(httpserver)
    assert len(request["input"]) == 20  # one per sentence
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]


# --- a batch with structured and chunking ---


def test_batch_with_structured_and_chunking(service: LlmService, httpserver: HTTPServer) -> None:
    body = {
        "urls": [PAGE_URL],
        "structured": {"schema": schema(name=STRING, merksatz=STRING)},
        "chunking": {"strategy": "sentence", "size": 400, "overlap": 0},
    }
    with TestClient(service.app) as client, Workers(service, extract=True):
        response = client.post("/v1/batch", json=body, headers=USER_LLM)
        assert response.status_code == 202, response.text
        job = service.wait_for_job(client, response.json()["data"]["id"])

    assert job["data"]["state"] == "completed"
    [item] = job["data"]["items"]
    result = item["result"]
    assert result["status"] == "success", result["error"]
    data = result["data"]
    assert data["structured"]["values"] == {"name": TITLE, "merksatz": MERKSATZ}
    assert data["structured"]["sources"] == {"name": "metadata", "merksatz": "llm"}
    assert_chunks(data["content"], data["chunks"], size=400, overlap=0)
    [request] = chat_requests(httpserver)
    assert asked_fields(request) == ["merksatz"]
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]
    assert CALLER_KEY not in json.dumps(job)
    assert service.stored("job:*:llm_key") == []  # the batch's last result dropped the key
