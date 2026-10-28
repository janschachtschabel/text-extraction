"""The PII child (``pii/child.py``) and its recognizers (``pii/recognizers.py``).

The default suite runs the child's protocol against a fake engine: it needs no Presidio and
no model. Marker ``nlp`` (``uv sync --extra nlp``, then ``uv run pytest -m nlp``) runs the real
children through a ``DocumentPool`` with the models of ``PII_TEST_MODEL_SIZE`` (default ``md``;
``lg`` for the larger ones), downloaded into ``PII_TEST_MODELS_PATH`` on first use.
"""

import asyncio
import importlib
import importlib.util
import subprocess
import sys
import textwrap
import threading
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from tests.pii.conftest import NlpModels
from text_extraction import model_store
from text_extraction.documents import child_logging
from text_extraction.documents.pool import ConversionError, DocumentPool
from text_extraction.pii.types import model_names

HAS_NLP = (
    importlib.util.find_spec("presidio_analyzer") is not None
    and importlib.util.find_spec("spacy") is not None
)


def nlp_test(function: Callable[..., Any]) -> Callable[..., Any]:
    """A test of the real children: marker ``nlp``, skipped without the extra ``nlp``."""
    skip = pytest.mark.skipif(not HAS_NLP, reason="the extra nlp is not installed")
    marked: Callable[..., Any] = pytest.mark.nlp(skip(function))
    return marked


# --- the protocol, with a fake engine (default suite) ---------------------------------------------


@dataclass
class Found:
    start: int
    end: int
    entity_type: str
    score: float


class FakeEngine:
    """Finds every ``needle@example.org`` as an e-mail address and every ``Anna`` as a person
    (0.85), whatever it is asked for or the threshold is: the child must filter itself. Remembers
    what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def analyze(
        self, *, text: str, language: str, entities: list[str], score_threshold: float
    ) -> list[Found]:
        self.calls.append(
            {"text": text, "language": language, "entities": entities, "threshold": score_threshold}
        )
        found = []
        for word, kind, score in (
            ("needle@example.org", "EMAIL_ADDRESS", 1.0),
            ("Anna", "PERSON", 0.85),
        ):
            start = text.find(word)
            while start != -1:
                found.append(Found(start, start + len(word), kind, score))
                start = text.find(word, start + 1)
        if text:
            found.append(Found(0, 1, "LOCATION", 0.3))
        return found


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The child module, freshly imported: importing it silences logging process-wide, which
    another test must not inherit (the module is imported with the silencing off)."""
    monkeypatch.setattr(child_logging, "silence_logging", lambda: None)
    monkeypatch.delitem(sys.modules, "text_extraction.pii.child", raising=False)
    module: Any = importlib.import_module("text_extraction.pii.child")
    yield module
    module._ANALYZER = None


def test_analyze_before_load_is_a_clear_error(child: Any) -> None:
    with pytest.raises(child.NotLoaded, match="load"):
        child.analyze([("text", "Anna")], "de", ["PERSON"], 0.4)


def test_analyze_returns_spans_per_field_name(child: Any) -> None:
    engine = FakeEngine()
    child._ANALYZER = engine

    reply = child.analyze(
        [("content", "Hallo Anna, mail an needle@example.org"), ("title", "ohne"), ("meta", "")],
        "de",
        ["PERSON", "EMAIL_ADDRESS"],
        0.4,
    )

    assert {name: sorted(spans) for name, spans in reply.items()} == {
        "content": [(6, 10, "PERSON", 0.85), (20, 38, "EMAIL_ADDRESS", 1.0)],
        "title": [],
        "meta": [],
    }
    assert engine.calls[0]["language"] == "de"
    assert engine.calls[0]["entities"] == ["PERSON", "EMAIL_ADDRESS"]
    assert engine.calls[0]["threshold"] == 0.4


def test_only_asked_types_at_or_above_the_threshold(child: Any) -> None:
    child._ANALYZER = FakeEngine()
    text = "Anna schreibt an needle@example.org"

    assert child.analyze([("a", text)], "en", ["PERSON"], 0.4)["a"] == [(0, 4, "PERSON", 0.85)]
    assert child.analyze([("a", text)], "en", ["PERSON"], 0.85)["a"] == [(0, 4, "PERSON", 0.85)]
    assert child.analyze([("a", text)], "en", ["PERSON"], 0.9)["a"] == []
    assert child.analyze([("a", text)], "en", ["LOCATION"], 0.2)["a"] == [(0, 1, "LOCATION", 0.3)]
    assert child.analyze([("a", text)], "en", [], 0.0) == {"a": []}  # nothing asked, nothing run


def test_long_text_goes_in_segments_with_offsets_added_back(child: Any) -> None:
    engine = FakeEngine()
    child._ANALYZER = engine
    filler = ("lorem ipsum dolor sit amet " * 1000).rstrip()  # 26,999 characters
    text = f"{filler}\n\nAnna schreibt an needle@example.org.\n{filler}"

    spans = child.analyze([("body", text)], "de", ["PERSON", "EMAIL_ADDRESS"], 0.4)["body"]

    assert len(engine.calls) > 3  # cut in spaces, at the blank line and at a line break
    assert all(len(call["text"]) <= 20_000 for call in engine.calls)
    assert [(text[start:end], kind) for start, end, kind, _ in sorted(spans)] == [
        ("Anna", "PERSON"),
        ("needle@example.org", "EMAIL_ADDRESS"),
    ]


def test_field_names_must_be_unique(child: Any) -> None:
    child._ANALYZER = FakeEngine()

    with pytest.raises(ValueError, match="unique"):
        child.analyze([("a", "x"), ("a", "y")], "de", ["PERSON"], 0.4)


def test_only_german_and_english_are_analysed(child: Any) -> None:
    child._ANALYZER = FakeEngine()

    with pytest.raises(ValueError, match="language"):
        child.analyze([("a", "x")], "fr", ["PERSON"], 0.4)


def test_functions_take_the_payload_the_pool_sends(child: Any) -> None:
    child._ANALYZER = FakeEngine()

    reply = child.FUNCTIONS["analyze"](
        {"fields": [["a", "Anna"]], "language": "en", "entities": ["PERSON"], "threshold": 0.4}
    )

    assert reply == {"a": [(0, 4, "PERSON", 0.85)]}
    assert set(child.FUNCTIONS) == {"load", "analyze"}


def test_load_without_a_model_is_models_missing(child: Any, tmp_path: Path) -> None:
    names = model_names("md")
    (tmp_path / names["de"]).mkdir()
    (tmp_path / names["de"] / "config.cfg").write_text("[nlp]")
    (tmp_path / names["de"] / "meta.json").write_text("{}")
    (tmp_path / names["en"]).mkdir()
    (tmp_path / names["en"] / "meta.json").write_text("{}")  # no config.cfg

    with pytest.raises(model_store.ModelsMissing, match="1 of 2"):
        child.load(str(tmp_path), "md")
    with pytest.raises(model_store.ModelsMissing, match="2 of 2"):
        child.load(str(tmp_path / "empty"), "md")
    with pytest.raises(ValueError, match="size"):
        child.load(str(tmp_path), "sm")
    assert child._ANALYZER is None


def test_a_loaded_child_keeps_its_analyzer(child: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load`` builds the engine once (``build_analyzer`` of ``recognizers``) with both
    languages' model directories; ``analyze`` uses it for every later call."""
    built: list[dict[str, Path]] = []
    engine = FakeEngine()

    def build(models: dict[str, Path]) -> FakeEngine:
        built.append(dict(models))
        return engine

    names = model_names("lg")
    root = Path("/models")
    monkeypatch.setattr(model_store, "require_models", lambda *_: None)
    monkeypatch.setitem(
        sys.modules, "text_extraction.pii.recognizers", types.SimpleNamespace(build_analyzer=build)
    )

    child.load(str(root), "lg")
    child.analyze([("a", "Anna")], "en", ["PERSON"], 0.4)
    child.analyze([("a", "Anna")], "de", ["PERSON"], 0.4)

    assert built == [{"de": root / names["de"], "en": root / names["en"]}]
    assert len(engine.calls) == 2


def fake_models(
    monkeypatch: pytest.MonkeyPatch, before_build: Callable[[], None] | None = None
) -> list[tuple[dict[str, Path], "FakeEngine"]]:
    """``build_analyzer`` and the volume check replaced: the engines built, with their models;
    ``before_build`` runs when an engine is about to be built (it may raise)."""
    built: list[tuple[dict[str, Path], FakeEngine]] = []

    def build(models: dict[str, Path]) -> FakeEngine:
        if before_build is not None:
            before_build()
        engine = FakeEngine()
        built.append((dict(models), engine))
        return engine

    monkeypatch.setattr(model_store, "require_models", lambda *_: None)
    monkeypatch.setitem(
        sys.modules, "text_extraction.pii.recognizers", types.SimpleNamespace(build_analyzer=build)
    )
    return built


def lazy_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fields": [["a", "Anna"]],
        "language": "en",
        "entities": ["PERSON"],
        "threshold": 0.4,
        "models_path": "/models",
        "size": "md",
    }
    payload.update(changes)
    return payload


def test_analyze_loads_the_models_of_its_payload_once(
    child: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R12: a child loads lazily, in the call that finds it without models, and not again
    for the same path and size, however many calls follow or a ``load`` repeats."""
    built = fake_models(monkeypatch)

    first = child.FUNCTIONS["analyze"](lazy_payload())
    child.FUNCTIONS["analyze"](lazy_payload())
    child.FUNCTIONS["load"]({"models_path": "/models", "size": "md"})

    assert first == {"a": [(0, 4, "PERSON", 0.85)]}
    assert [models for models, _engine in built] == [
        {"de": Path("/models") / "de_core_news_md", "en": Path("/models") / "en_core_web_md"}
    ]


def test_analyze_for_other_models_drops_the_old_engine_before_building_the_next(
    child: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_during_build: list[Any] = []
    # Two engines alive at once would roughly double the memory.
    built = fake_models(monkeypatch, lambda: seen_during_build.append(child._ANALYZER))
    child.FUNCTIONS["analyze"](lazy_payload())

    child.FUNCTIONS["analyze"](lazy_payload(size="lg"))
    child.FUNCTIONS["analyze"](lazy_payload(size="lg"))

    assert len(built) == 2
    assert seen_during_build == [None, None]  # nothing was alive at either build
    assert child._LOADED_FOR == ("/models", "lg")


def test_a_memory_error_while_loading_is_load_failed_not_a_text_problem(
    child: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def out_of_memory() -> None:
        raise MemoryError

    fake_models(monkeypatch, out_of_memory)

    with pytest.raises(child.LoadFailed):
        child.FUNCTIONS["analyze"](lazy_payload())

    assert child._ANALYZER is None  # the next call tries again


def test_analyze_without_models_in_its_payload_needs_a_loaded_child(child: Any) -> None:
    payload = lazy_payload()
    del payload["models_path"], payload["size"]

    with pytest.raises(child.NotLoaded):
        child.FUNCTIONS["analyze"](payload)


def test_importing_the_child_silences_what_libraries_log() -> None:
    """What Presidio or spaCy log or warn may quote the text: in a child it reaches neither
    stderr (the pod's log stream) nor stdout."""
    code = textwrap.dedent(
        """
        import logging, warnings, structlog
        import text_extraction.pii.child
        logging.getLogger('presidio-analyzer').warning('leak %s', 'secret')
        logging.getLogger().error('leak')
        warnings.warn('leak secret')
        structlog.get_logger('x').error('leak', text='secret')
        """
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )

    assert out.stderr == ""
    assert out.stdout == ""


def test_parent_does_not_import_presidio_or_spacy() -> None:
    """The nlp worker's parent imports the pure PII modules, never Presidio or spaCy (their
    children do): the API and the other workers carry neither."""
    modules = [
        "text_extraction.pii.fields",
        "text_extraction.pii.html",
        "text_extraction.pii.spans",
        "text_extraction.pii.replace",
        "text_extraction.pii.types",
        "text_extraction.model_store",
        "text_extraction.documents.pool",
        # Task 5 adds text_extraction.nlp_worker here once it exists.
    ]
    code = (
        "import sys\n"
        + "".join(f"import {name}\n" for name in modules)
        + "leaked = sorted(m for m in sys.modules if m.split('.')[0] in "
        + "('presidio_analyzer', 'spacy', 'thinc', 'tldextract'))\n"
        + "print(leaked)\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"


# --- the real children (marker nlp) ---------------------------------------------------------------

REPO_ROOT = str(Path(__file__).resolve().parents[2])
DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "IP_ADDRESS",
]
THRESHOLD = 0.4  # PII_SCORE_THRESHOLD's default (docs/superpowers/plans/…-package-7-calibration.md)


class Children:
    """A ``DocumentPool`` of one real PII child on an event loop of its own, driven from the
    synchronous tests: the child loads the models once, every test reuses it."""

    def __init__(self, models: NlpModels, target: str = "text_extraction.pii.child") -> None:
        self._models = models
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        # The limit is the worker's default (PII_MEMORY_LIMIT_MB).
        self._pool = DocumentPool(
            1, 3072, target=target, extra_env={"PYTHONPATH": REPO_ROOT}, start_timeout=120
        )
        self._wait(self._pool.start())
        self.call("load", {"models_path": str(models.path), "size": models.size})

    def _wait(self, work: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(work, self._loop).result(timeout=600)

    def call(self, function: str, payload: dict[str, Any]) -> Any:
        async def run() -> Any:
            return await self._pool.run(function, payload, self._loop.time() + 300)

        return self._wait(run())

    def spans(
        self,
        text: str,
        language: str,
        entities: list[str] | None = None,
        threshold: float = THRESHOLD,
    ) -> list[tuple[str, str]]:
        """What the worker would replace in ``text``: the resolved spans as ``(text, type)``."""
        from text_extraction.pii.spans import resolve
        from text_extraction.pii.types import Span

        reply = self.call(
            "analyze",
            {
                "fields": [("text", text)],
                "language": language,
                "entities": entities or DEFAULT_ENTITIES,
                "threshold": threshold,
            },
        )
        found = [Span(start, end, kind, score) for start, end, kind, score in reply["text"]]
        return [(text[span.start : span.end], span.type) for span in resolve(found, text)]

    def close(self) -> None:
        self._wait(self._pool.shutdown())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def children(nlp_models: NlpModels) -> Iterator[Children]:
    pool = Children(nlp_models)
    yield pool
    pool.close()


def persons(found: list[tuple[str, str]]) -> list[str]:
    return [text for text, kind in found if kind == "PERSON"]


@nlp_test
def test_german_persons_found(children: Children) -> None:
    text = (
        "Dr. Anna Schmidt leitet das Projekt. Für Rückfragen steht Ihnen Herr Müller zur "
        "Verfügung. Anna-Lena Meyer-Schulz hat die Aufgabe übernommen."
    )

    found = persons(children.spans(text, "de"))

    assert "Müller" in found  # the span is the name: `Herr [PERSON]`
    assert any("Anna Schmidt" in name for name in found)
    assert any(name.endswith("Meyer-Schulz") for name in found)


@nlp_test
def test_german_phone_iban_email(children: Children) -> None:
    text = (
        "Telefonisch erreichen Sie uns unter +49 30 901820. Das Konto: "
        "IBAN DE89 3704 0044 0532 0130 00. Schreiben Sie an kontakt@example.org."
    )

    found = children.spans(text, "de")

    assert ("+49 30 901820", "PHONE_NUMBER") in found
    assert ("DE89 3704 0044 0532 0130 00", "IBAN_CODE") in found
    assert ("kontakt@example.org", "EMAIL_ADDRESS") in found


@nlp_test
def test_english_person_email_phone_card(children: Children) -> None:
    text = (
        "Please contact Dr. Sarah Connor at sarah.connor@example.com or call +1 202-456-1111. "
        "Pay with the card 4111 1111 1111 1111."
    )

    found = children.spans(text, "en")

    assert any("Sarah Connor" in name for name in persons(found))
    assert ("sarah.connor@example.com", "EMAIL_ADDRESS") in found
    assert ("+1 202-456-1111", "PHONE_NUMBER") in found
    assert ("4111 1111 1111 1111", "CREDIT_CARD") in found


@nlp_test
def test_capitalised_nouns_not_persons(children: Children) -> None:
    text = "Die Energiewende und die Photosynthese sind wichtige Themen im Unterricht."

    assert children.spans(text, "de") == []


@nlp_test
def test_segment_border_entity_found(children: Children) -> None:
    """An address that straddles character 20,000 stays whole: the text is cut at a space before
    it, and its offsets are the text's, not the segment's."""
    email = "grenze@example.org"
    head = "wort " * 3998  # 19,990 characters: the address starts 10 before the border
    text = f"{head}{email} und noch ein Satz danach. " + "wort " * 100
    assert text.index(email) < 20_000 < text.index(email) + len(email)
    reply = children.call(
        "analyze",
        {
            "fields": [("body", text)],
            "language": "de",
            "entities": ["EMAIL_ADDRESS"],
            "threshold": 0.4,
        },
    )

    assert [(text[start:end], kind) for start, end, kind, _ in reply["body"]] == [
        (email, "EMAIL_ADDRESS")
    ]
    assert reply["body"][0][:2] == (text.index(email), text.index(email) + len(email))


@nlp_test
def test_field_names_are_the_names_given(children: Children) -> None:
    reply = children.call(
        "analyze",
        {
            "fields": [("title", "Mail an a@example.org"), ("content", "nichts"), ("alt 1", "")],
            "language": "de",
            "entities": ["EMAIL_ADDRESS"],
            "threshold": 0.4,
        },
    )

    assert set(reply) == {"title", "content", "alt 1"}
    assert [span[:3] for span in reply["title"]] == [(8, 21, "EMAIL_ADDRESS")]
    assert reply["content"] == reply["alt 1"] == []


@nlp_test
def test_an_unwanted_type_is_not_returned(children: Children) -> None:
    text = "Anna Schmidt aus Berlin schreibt an anna@example.org."

    found = children.spans(text, "de", ["EMAIL_ADDRESS"])

    assert found == [("anna@example.org", "EMAIL_ADDRESS")]
    assert ("Berlin", "LOCATION") in children.spans(text, "de", ["LOCATION"])


@nlp_test
def test_load_with_the_wrong_path_is_models_missing(nlp_models: NlpModels, tmp_path: Path) -> None:
    pool = Children.__new__(Children)  # a child that is loaded by the test itself
    pool._models = nlp_models
    pool._loop = asyncio.new_event_loop()
    pool._thread = threading.Thread(target=pool._loop.run_forever, daemon=True)
    pool._thread.start()
    pool._pool = DocumentPool(
        1, 3072, target="text_extraction.pii.child", extra_env={"PYTHONPATH": REPO_ROOT}
    )
    pool._wait(pool._pool.start())
    try:
        with pytest.raises(ConversionError) as caught:
            pool.call("load", {"models_path": str(tmp_path), "size": nlp_models.size})
        assert caught.value.type_name == "ModelsMissing"  # the type name the worker maps
        with pytest.raises(ConversionError) as not_loaded:
            pool.call(
                "analyze",
                {
                    "fields": [("a", "x")],
                    "language": "de",
                    "entities": ["PERSON"],
                    "threshold": 0.4,
                },
            )
        assert not_loaded.value.type_name == "NotLoaded"
    finally:
        pool.close()


@nlp_test
def test_no_network_for_email(nlp_models: NlpModels) -> None:
    """tldextract validates an address's domain against its bundled suffix list: the child never
    reaches publicsuffix.org (here every connection attempt is refused and counted), and the
    address is found all the same."""
    pool = Children(nlp_models, target="tests.pii.nlp_targets")
    try:
        found = pool.spans("Mail an kontakt@example.org oder info@example.net.", "de")
        attempts = pool.call("connect_attempts", {})
    finally:
        pool.close()

    assert ("kontakt@example.org", "EMAIL_ADDRESS") in found
    assert ("info@example.net", "EMAIL_ADDRESS") in found
    assert attempts == []


@nlp_test
@pytest.mark.parametrize(("language", "file"), [("de", "check_de.txt"), ("en", "check_en.txt")])
def test_the_built_in_check_sentences_hold_a_name_and_an_email(
    children: Children, language: str, file: str
) -> None:
    text = resources.files("text_extraction.pii").joinpath(file).read_text(encoding="utf-8")

    kinds = {kind for _, kind in children.spans(text, language)}

    assert {"PERSON", "EMAIL_ADDRESS"} <= kinds


@nlp_test
def test_presidio_stays_quiet_and_offline_by_configuration(nlp_models: NlpModels) -> None:
    import logging

    from text_extraction.pii.recognizers import build_analyzer

    names = model_names(nlp_models.size)
    build_analyzer({"de": nlp_models.path / names["de"], "en": nlp_models.path / names["en"]})

    assert logging.getLogger("presidio-analyzer").level == logging.WARNING
    assert logging.getLogger("tldextract").level == logging.WARNING
