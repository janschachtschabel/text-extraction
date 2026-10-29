"""The done-when test of package 7: the PII corpus (``tests/pii/corpus``) through the real service.

Every text goes through ``POST /v1/extract/file`` as Markdown (an upload of the text, ``format:
markdown``) and as HTML (the text wrapped in a minimal HTML document, ``format: html``), with every
mode the format allows, and through the real nlp worker loop: the real ``Worker`` and ``PiiRunner``
on a shared fakeredis server with the real children (Presidio, spaCy and the models of
``PII_TEST_MODEL_SIZE``, default ``md``; ``lg`` weekly in CI). The API and the worker are those of
``tests/test_pii_dispatch.py``.

Per mode the answer has its form: ``mask`` writes ``[<TYPE>]``, ``redact`` ``[REDACTED]``, ``hash``
``[<TYPE>:<12 hex>]`` (equal for equal values within a result), ``annotate`` leaves the text as
it is and its offsets point at the entity. Recall per type is that of ``annotate`` over the
Markdown texts, counted as in ``docs/superpowers/plans/2026-10-26-package-7-calibration.md``: a
gold span is found when entities of its type cover 80 % of it. The pattern types must be found
without a miss; ``PERSON`` meets the pins below. No word of a ``keep`` list is touched by the
default entity types (decision D2: places and institutions are not personal data), except the
known false positives listed per model size in ``KNOWN_FALSE_POSITIVES``.

Run it with ``uv sync --extra nlp`` and ``uv run pytest -m nlp tests/pii/test_corpus.py`` (add
``PII_TEST_MODEL_SIZE=lg`` for the large models).
"""

import html
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from lxml import html as lxml_html

from tests.pii.conftest import NlpModels
from tests.pii.test_child import HAS_NLP
from tests.test_pii_dispatch import AUTH, PiiService
from text_extraction import nlp_worker

pytestmark = [
    pytest.mark.nlp,
    pytest.mark.skipif(not HAS_NLP, reason="the extra nlp is not installed"),
]

CORPUS: Final = Path(__file__).parent / "corpus"
GOLD: Final[dict[str, dict[str, Any]]] = json.loads((CORPUS / "gold.json").read_text("utf-8"))
NAMES: Final = sorted(GOLD)
TEXTS: Final = {name: (CORPUS / name).read_text("utf-8") for name in NAMES}

PATTERN_TYPES: Final = ("EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD", "IP_ADDRESS")
# The types asked for by default (D2) that the corpus has gold spans for; ``URL`` is asked for only.
GOLD_TYPES: Final = ("PERSON", *PATTERN_TYPES)
LANGUAGES: Final = ("de", "en")

# Least recall of ``PERSON`` per model size and language (rounded down, two decimals). ``lg``: the
# calibration's pins (the calibration doc, "Numbers that Task 8 pins"); ``md``: measured with the
# real recognizers and this test (the calibration doc, "md pins (Task 8)"). The pattern types
# have no pin: they must be found without a miss, with either size.
PERSON_PINS: Final[dict[str, dict[str, float]]] = {
    "lg": {"de": 0.96, "en": 1.00},
    "md": {"de": 1.00, "en": 1.00},
}

# Keep words that the NER takes for a ``PERSON`` all the same (measured in the calibration, finding
# 7, and again by this test): historical names used as concepts ("Satz des Pythagoras", "Calvin
# cycle"). Masking them costs precision, not privacy, so the test names them per model size, as
# (file, word), instead of leaving them out of ``gold.json``. Any other keep word that is touched
# fails the test, and so does a listed word that is no longer touched (the list must not go stale).
KNOWN_FALSE_POSITIVES: Final[dict[str, frozenset[tuple[str, str]]]] = {
    "lg": frozenset(
        {
            ("de_04_forum_post.md", "Pythagoras"),
            ("en_02_worksheet_credits.md", "Calvin"),
            # A sentence in another language of the Latin script (Turkish): the script filter of
            # issue #14 cannot tell it apart, and the German model takes its first words for a name.
            ("de_08_multilingual.md", "Okulumuzdaki"),
        }
    ),
    "md": frozenset(
        {
            ("de_04_forum_post.md", "Pythagoras"),
            ("en_02_worksheet_credits.md", "Calvin"),
            ("en_04_forum_post.md", "Pythagoras"),
        }
    ),
}

HASH_TOKEN: Final = re.compile(r"\[([A-Z_]+):([0-9a-f]{12})\]")


def language_of(name: str) -> str:
    return name.split("_", 1)[0]


def html_page(text: str) -> str:
    """The fixture of the HTML runs: one paragraph per block of the text, escaped."""
    blocks = [block for block in text.split("\n\n") if block.strip()]
    paragraphs = "".join(f"<p>{html.escape(block)}</p>" for block in blocks)
    return (
        '<!doctype html><html lang="und"><head><meta charset="utf-8"><title>Corpus</title></head>'
        f"<body>{paragraphs}</body></html>"
    )


def html_text(content: str) -> str:
    """The text of an HTML answer: its text nodes, tags dropped, entities decoded."""
    return str(lxml_html.fromstring(content).text_content())


def normalized(value: str) -> str:
    """A value as ``hash`` keys it (N2): case-folded, whitespace collapsed."""
    return " ".join(value.casefold().split())


@dataclass
class Served:
    """The public app on fakeredis with the real nlp worker, and the answers it has given."""

    client: TestClient
    answers: dict[tuple[str, str, str | None], dict[str, Any]] = field(default_factory=dict)

    def answer(self, name: str, fmt: str, mode: str | None) -> dict[str, Any]:
        """The body of the upload of corpus text ``name`` as ``fmt``, with PII ``mode`` (None:
        without ``pii``). Each is asked once."""
        key = (name, fmt, mode)
        if key not in self.answers:
            self.answers[key] = self.post(name, fmt, mode).json()
        return self.answers[key]

    def post(self, name: str, fmt: str, mode: str | None) -> Any:
        if fmt == "html":
            body, file_name, content_type = html_page(TEXTS[name]), "page.html", "text/html"
        else:
            body, file_name, content_type = TEXTS[name], name, "text/plain"
        form = {"format": fmt}
        if mode is not None:
            form["pii"] = mode
        return self.client.post(
            "/v1/extract/file",
            files={"file": (file_name, body.encode(), content_type)},
            data=form,
            headers=AUTH,
        )

    def data(self, name: str, fmt: str, mode: str | None) -> dict[str, Any]:
        body = self.answer(name, fmt, mode)
        assert body["status"] == "success", (name, fmt, mode, body["error"])
        data: dict[str, Any] = body["data"]
        return data


@pytest.fixture(scope="module")
def served(nlp_models: NlpModels) -> Iterator[Served]:
    service = PiiService(
        pii_models_path=nlp_models.path,
        pii_model_size=nlp_models.size,
        pii_timeout_seconds=120,  # the first item of a child loads both models
        pii_sync_wait_seconds=60,
    )
    pool = nlp_worker.create_nlp_pool(service.settings)
    with TestClient(service.app) as client, service.nlp(pool):
        yield Served(client)


@pytest.fixture(scope="module")
def size(nlp_models: NlpModels) -> str:
    return nlp_models.size


def annotated(served: Served, name: str) -> tuple[str, list[dict[str, Any]]]:
    """The content of the ``annotate`` answer for ``name`` and its items of that content."""
    data = served.data(name, "markdown", "annotate")
    items = data["pii"]["items"]
    return data["content"], [item for item in items if item["field"] == "content"]


def covered(spans: list[tuple[int, int]], start: int, end: int) -> int:
    """How many code points of ``start:end`` the spans cover."""
    cells: set[int] = set()
    for first, last in spans:
        cells.update(range(max(first, start), min(last, end)))
    return len(cells)


def found_by_type(served: Served, languages: tuple[str, ...]) -> dict[tuple[str, str], list[bool]]:
    """Per language and type, whether each gold span is found (80 % of it covered by the
    entities of its type)."""
    found: dict[tuple[str, str], list[bool]] = {
        (language, kind): [] for language in languages for kind in GOLD_TYPES
    }
    for name in NAMES:
        language = language_of(name)
        if language not in languages:
            continue
        _, items = annotated(served, name)
        for gold in GOLD[name]["spans"]:
            if gold["type"] not in GOLD_TYPES:
                continue
            spans = [(i["start"], i["end"]) for i in items if i["type"] == gold["type"]]
            length = gold["end"] - gold["start"]
            hit = covered(spans, gold["start"], gold["end"]) >= 0.8 * length
            found[(language, gold["type"])].append(hit)
    return found


# --- every mode, per text -------------------------------------------------------------------------


def template(
    content: str, items: list[dict[str, Any]], piece: Callable[[dict[str, Any]], str]
) -> str:
    """``content`` with every item replaced by ``piece(item)``."""
    out: list[str] = []
    position = 0
    for item in sorted(items, key=lambda i: i["start"]):
        assert item["start"] >= position, "the entities of one field must not overlap"
        out += [content[position : item["start"]], piece(item)]
        position = item["end"]
    out.append(content[position:])
    return "".join(out)


@pytest.mark.parametrize("name", NAMES)
def test_annotate_leaves_the_text_and_its_offsets_point_at_the_entities(
    served: Served, name: str
) -> None:
    text = TEXTS[name]
    data = served.data(name, "markdown", "annotate")
    content, items = annotated(served, name)

    assert content == text.rstrip("\n")  # nothing was replaced (the extraction trims the end)
    report = data["pii"]
    assert report["mode"] == "annotate"
    assert report["language"] == language_of(name)
    assert report["hash_scope"] is None
    assert "pii_language_unsupported" not in data["warnings"]
    for item in items:
        assert content[item["start"] : item["end"]] == item["text"]
        assert 0.4 <= item["score"] <= 1.0
    counts: dict[str, int] = {}
    for item in report["items"]:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    assert counts == report["entities"]
    # The gold spans that were found are at the gold offsets.
    for gold in GOLD[name]["spans"]:
        assert text[gold["start"] : gold["end"]] == gold["text"]  # the gold file's own check
        if gold["type"] in PATTERN_TYPES:
            assert (gold["start"], gold["end"], gold["type"]) in {
                (i["start"], i["end"], i["type"]) for i in items
            }, gold


@pytest.mark.parametrize("name", NAMES)
def test_mask_and_redact_write_their_brackets_where_annotate_lists_the_entities(
    served: Served, name: str
) -> None:
    content, items = annotated(served, name)
    for mode, piece in (
        ("mask", lambda item: f"[{item['type']}]"),
        ("redact", lambda item: "[REDACTED]"),
    ):
        data = served.data(name, "markdown", mode)

        assert data["content"] == template(content, items, piece), mode
        assert data["pii"]["mode"] == mode
        assert data["pii"]["items"] is None
        assert data["pii"]["hash_scope"] is None
        assert data["pii"]["entities"] == {
            kind: sum(1 for item in items if item["type"] == kind)
            for kind in sorted({item["type"] for item in items})
        }


@pytest.mark.parametrize("name", NAMES)
def test_hash_writes_equal_hashes_for_equal_values(served: Served, name: str) -> None:
    content, items = annotated(served, name)
    data = served.data(name, "markdown", "hash")

    assert data["pii"]["hash_scope"] == "result"  # no PII_HASH_KEY in this service
    assert data["pii"]["items"] is None
    # Every entity is a token of its own type; the text between them is untouched.
    ordered = sorted(items, key=lambda i: i["start"])
    parts: list[str] = []
    position = 0
    for item in ordered:
        parts += [
            re.escape(content[position : item["start"]]),
            rf"\[{item['type']}:([0-9a-f]{{12}})\]",
        ]
        position = item["end"]
    parts.append(re.escape(content[position:]))
    match = re.fullmatch("".join(parts), data["content"], re.DOTALL)
    assert match is not None, "the hash form is [<TYPE>:<12 hex>] at each entity"
    hashes = list(match.groups())
    assert len(hashes) == len(items)
    by_value: dict[tuple[str, str], set[str]] = {}
    for item, digest in zip(ordered, hashes, strict=True):
        by_value.setdefault((item["type"], normalized(item["text"])), set()).add(digest)
    assert all(len(digests) == 1 for digests in by_value.values()), by_value  # equal -> equal
    distinct = [next(iter(digests)) for digests in by_value.values()]
    assert len(set(distinct)) == len(distinct)  # different values differ (12 hex digits)


@pytest.mark.parametrize("name", NAMES)
def test_html_mask_redact_and_hash_replace_the_entities_in_the_text_nodes(
    served: Served, name: str
) -> None:
    _, items = annotated(served, name)
    pattern_items = [item for item in items if item["type"] in PATTERN_TYPES]
    for mode in ("mask", "redact", "hash"):
        data = served.data(name, "html", mode)
        text = html_text(data["content"])

        assert data["pii"]["mode"] == mode
        assert data["pii"]["language"] == language_of(name)
        assert data["pii"]["items"] is None
        assert data["pii"]["hash_scope"] == ("result" if mode == "hash" else None)
        assert "<p>" in data["content"]  # still HTML, escaped where it was text
        for item in pattern_items:  # a pattern is found whatever the markup
            assert item["text"] not in text, (mode, item)
        entities = data["pii"]["entities"]
        for kind in {item["type"] for item in pattern_items}:
            assert entities.get(kind, 0) >= sum(1 for i in pattern_items if i["type"] == kind)
        if mode == "mask":
            for kind, count in entities.items():
                assert text.count(f"[{kind}]") == count
            assert not HASH_TOKEN.search(text)
            assert "[REDACTED]" not in text
        elif mode == "redact":
            assert text.count("[REDACTED]") == sum(entities.values())
            assert not re.search(r"\[(?:[A-Z_]+)\]", text.replace("[REDACTED]", ""))
        else:
            tokens = HASH_TOKEN.findall(text)
            assert len(tokens) == sum(entities.values())
            values = {(i["type"], normalized(i["text"])) for i in pattern_items}
            patterns = {t for t in tokens if t[0] in PATTERN_TYPES}
            assert len(patterns) == len(values)  # one hash per value, whatever its place


def test_annotate_with_html_is_refused(served: Served) -> None:
    response = served.post(NAMES[0], "html", "annotate")

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "annotate needs format markdown or text."


# --- recall and keep words ------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", GOLD_TYPES)
def test_recall_per_type_meets_the_pins(
    served: Served, size: str, language: str, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    hits = found_by_type(served, (language,))[(language, kind)]
    recall = sum(hits) / len(hits)
    with capsys.disabled():
        print(f"\nRECALL {size} {language} {kind}: {sum(hits)}/{len(hits)} = {recall:.4f}")

    if kind in PATTERN_TYPES:
        assert all(hits), f"{kind} ({language}, {size}): {sum(hits)} of {len(hits)} found"
    else:
        pin = PERSON_PINS[size][language]
        assert recall >= pin, f"{kind} ({language}, {size}): {sum(hits)} of {len(hits)} found"


@pytest.mark.parametrize("name", NAMES)
def test_no_keep_word_is_touched_by_the_default_entities(
    served: Served, size: str, name: str
) -> None:
    text = TEXTS[name]
    _, items = annotated(served, name)
    touched: list[tuple[str, int, str, str]] = []
    for word in GOLD[name]["keep"]:
        for match in re.finditer(re.escape(word), text):
            for item in items:
                if item["start"] < match.end() and match.start() < item["end"]:
                    touched.append((word, match.start(), item["type"], item["text"]))

    known = {word for file, word in KNOWN_FALSE_POSITIVES[size] if file == name}
    assert not [hit for hit in touched if hit[0] not in known], touched
    # The list must not go stale: a known false positive that is no longer hit is removed from it.
    assert known <= {hit[0] for hit in touched}, (known, touched)


@pytest.mark.parametrize("name", NAMES)
def test_no_keep_word_is_touched_in_the_html_answers(served: Served, size: str, name: str) -> None:
    """A keep word is as often in the masked text as in the unmasked one (the known false
    positives of this size aside: the Markdown test above pins them)."""
    known = {word for file, word in KNOWN_FALSE_POSITIVES[size] if file == name}
    plain = html_text(served.data(name, "html", None)["content"])
    for mode in ("mask", "redact", "hash"):
        text_after = html_text(served.data(name, "html", mode)["content"])
        for word in GOLD[name]["keep"]:
            if word not in known:
                assert text_after.count(word) == plain.count(word), (mode, word)
