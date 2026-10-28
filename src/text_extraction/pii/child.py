"""The ``DocumentPool`` target of the PII children: ``DocumentPool(target=
"text_extraction.pii.child")`` (ruling N13).

The child keeps one Presidio ``AnalyzerEngine`` (``pii/recognizers.py``) for German and English,
built by ``load`` once, after the child is started, and used by every later ``analyze``. The
parent (``nlp_worker.py``) never imports this module's engine: ``presidio_analyzer`` and spaCy
are imported inside ``load`` only, after the child's memory limit is set. ``FUNCTIONS`` is the
allowlist of what a parent may call by name, each with the payload dict the pool sends:

- ``load`` (``models_path``, ``size``): the two spaCy models of ``size`` (``lg`` or ``md``,
  ``de_core_news_<size>`` and ``en_core_web_<size>``) from the directories of the same names
  under ``models_path``. A directory without ``config.cfg`` and ``meta.json`` raises
  ``ModelsMissing`` (``model_store.require_models``), an unknown size ``ValueError``.
- ``analyze`` (``fields``, ``language``, ``entities``, ``threshold``): ``fields`` are
  ``(name, text)`` pairs. The reply is ``{name: [(start, end, type, score), ...]}`` for every
  name given: spans of the asked ``entities`` only, at or above ``threshold``, with offsets into
  that field's text, not yet resolved against each other (``pii/spans.py``: ``resolve``).
  Without a ``load`` first it raises ``NotLoaded``.

A text is analysed in segments of at most 20,000 characters (``spans.segments``): Presidio's
context step grows faster than linearly with the length, and the offsets are added back.

Before anything else is imported, logging is silenced (``child_logging``): what Presidio, spaCy
and tldextract log or warn may quote the text. The child never logs text itself.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

from text_extraction.documents.child_logging import silence_logging

silence_logging()

# After the silencing: the libraries log or warn while they load.
from text_extraction import model_store  # noqa: E402
from text_extraction.pii.spans import segments  # noqa: E402
from text_extraction.pii.types import model_names  # noqa: E402

LANGUAGES: Final = ("de", "en")


class NotLoaded(Exception):  # noqa: N818 - a call out of order, named as such
    """``analyze`` before ``load``: the parent starts a child and loads it first."""


class _Found(Protocol):
    start: int
    end: int
    entity_type: str
    score: float


class _Analyzer(Protocol):
    """What the child needs of Presidio's ``AnalyzerEngine``."""

    def analyze(
        self, *, text: str, language: str, entities: list[str], score_threshold: float
    ) -> Sequence[_Found]: ...


_ANALYZER: _Analyzer | None = None


def load(models_path: str, size: str) -> None:
    """Build the engine from the two models of ``size`` under ``models_path`` and keep it."""
    global _ANALYZER
    names = model_names(size)  # type: ignore[arg-type]  # ValueError for an unknown size
    root = Path(models_path)
    model_store.require_models(root, model_store.load_manifest("nlp").only(*names.values()))
    from text_extraction.pii.recognizers import build_analyzer

    _ANALYZER = build_analyzer({"de": root / names["de"], "en": root / names["en"]})


def analyze(
    fields: list[tuple[str, str]], language: str, entities: list[str], threshold: float
) -> dict[str, list[tuple[int, int, str, float]]]:
    """The spans of ``entities`` at or above ``threshold`` in each field (see the module
    docstring)."""
    engine = _ANALYZER
    if engine is None:
        raise NotLoaded("the models are not loaded: call load first")
    if language not in LANGUAGES:
        raise ValueError(f"the language must be one of {LANGUAGES}")
    names = [name for name, _ in fields]
    if len(set(names)) != len(names):
        raise ValueError("field names must be unique")
    wanted = set(entities)
    reply: dict[str, list[tuple[int, int, str, float]]] = {name: [] for name in names}
    if not wanted:
        return reply
    asked = list(entities)
    for name, text in fields:
        spans = reply[name]
        for offset, piece in segments(text):
            if not piece.strip():
                continue
            for found in engine.analyze(
                text=piece, language=language, entities=asked, score_threshold=threshold
            ):
                if found.entity_type in wanted and found.score >= threshold:
                    spans.append(
                        (
                            offset + found.start,
                            offset + found.end,
                            found.entity_type,
                            float(found.score),
                        )
                    )
    return reply


def _load_call(payload: dict[str, Any]) -> None:
    load(payload["models_path"], payload["size"])


def _analyze_call(payload: dict[str, Any]) -> dict[str, list[tuple[int, int, str, float]]]:
    fields = [(name, text) for name, text in payload["fields"]]
    return analyze(fields, payload["language"], payload["entities"], payload["threshold"])


FUNCTIONS: dict[str, Callable[[Any], Any]] = {"load": _load_call, "analyze": _analyze_call}
