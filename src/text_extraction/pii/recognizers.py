"""The Presidio engine of the PII children (ruling N6). Imported in the children only: it brings
in Presidio, spaCy and tldextract, which the API and the parent of the NLP worker never load.

One ``AnalyzerEngine`` serves ``de`` and ``en``:

- **Names, places, organisations** come from the spaCy models through Presidio's
  ``SpacyRecognizer``: ``PER`` and ``PERSON`` are ``PERSON``, ``LOC`` and ``GPE`` are
  ``LOCATION``, ``ORG`` is ``ORGANIZATION``. Every other label is ignored (the German models
  have ``MISC``, the English ones dates, money and more).
- **Patterns** per language: e-mail address, phone number (regions ``DE``, ``AT``, ``CH``,
  ``GB``, ``US``; German context words that lift a number's score from 0.4 to 0.75), IBAN,
  credit card, IP address and URL.
- **Honorifics** (``HonorificRecognizer``): the NER models miss many German surnames after a
  salutation (``Herr Müller``), so a name after ``Herr``, ``Herrn``, ``Frau``, ``Hr.``, ``Fr.``
  (German) or ``Mr``, ``Mrs``, ``Ms``, ``Mx``, ``Dr`` (English), after a title alone (``Prof.``,
  ``Dr.``, ``Prof. Dr.``, ``Doktor``), or after a salutation and a title, is a ``PERSON`` of
  score 0.7. The span is the name only: ``Herr Müller`` becomes ``Herr [PERSON]``.

tldextract never goes online: Presidio's e-mail recognizer validates the domain with it, and
tldextract's default would fetch the public suffix list from publicsuffix.org (and cache it
on disk). ``OfflineEmailRecognizer`` uses ``TLDExtract(suffix_list_urls=(), cache_dir=None)``:
the list bundled with the package, no network, no file. Presidio's and tldextract's loggers are
set to ``WARNING``; the decision log is off.
"""

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

import tldextract
from presidio_analyzer import (
    AnalyzerEngine,
    LocalRecognizer,
    RecognizerRegistry,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    IpRecognizer,
    PhoneRecognizer,
    SpacyRecognizer,
    UrlRecognizer,
)

Language = Literal["de", "en"]

PHONE_REGIONS: Final = ("DE", "AT", "CH", "GB", "US")
GERMAN_PHONE_CONTEXT: Final = [
    "telefon",
    "tel",
    "telefonisch",
    "fon",
    "mobil",
    "handy",
    "fax",
    "rufnummer",
    "erreichbar",
]
# The labels of the German and English models that become one of our types; all else is ignored.
MODEL_ENTITIES: Final = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "LOC": "LOCATION",
    "GPE": "LOCATION",
    "ORG": "ORGANIZATION",
}
IGNORED_LABELS: Final = [
    "MISC",
    "DATE",
    "TIME",
    "NORP",
    "MONEY",
    "CARDINAL",
    "ORDINAL",
    "QUANTITY",
    "PERCENT",
    "EVENT",
    "FAC",
    "LAW",
    "LANGUAGE",
    "PRODUCT",
    "WORK_OF_ART",
]
HONORIFIC_SCORE: Final = 0.7
_RECOGNIZER_NAME: Final = "HonorificRecognizer"

_UPPER = "A-ZÀÁÂÄÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜÝ"
_LOWER = "a-zßàáâäçèéêëìíîïñòóôõöøùúûüýÿ"
_NAME_PART = rf"[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)*"  # Meyer-Schulz is one part
_NAME = rf"(?P<name>{_NAME_PART}(?:[ \t\xa0]+{_NAME_PART})?)"  # one or two words
_GAP = r"[ \t\xa0]+"
# Titles that may stand between a salutation and the name, or alone before it.
_TITLES = rf"(?:(?:(?:Dr|Prof)\.|Doktor){_GAP}){{1,2}}"
_SALUTATIONS: Final = {
    "de": r"(?:Herrn?|Frau|Hr\.|Fr\.)",
    "en": r"(?:Mr|Mrs|Ms|Mx|Dr)\.?",
}
_HONORIFICS: Final = {
    language: re.compile(rf"(?<![\w.])(?:{salutation}{_GAP}(?:{_TITLES})?|{_TITLES}){_NAME}")
    for language, salutation in _SALUTATIONS.items()
}


class HonorificRecognizer(LocalRecognizer):
    """A name after a salutation or a title is a ``PERSON`` of score 0.7; the span is the name."""

    def __init__(self, supported_language: Language) -> None:
        if supported_language not in _HONORIFICS:
            raise ValueError("the honorific recognizer knows de and en")
        self._pattern = _HONORIFICS[supported_language]
        super().__init__(
            supported_entities=["PERSON"],
            supported_language=supported_language,
            name=_RECOGNIZER_NAME,
        )

    def load(self) -> None:  # nothing to load: a regular expression
        pass

    def analyze(
        self,
        text: str,
        entities: list[str],
        nlp_artifacts: NlpArtifacts | None = None,
    ) -> list[RecognizerResult]:
        """The names the pattern finds, when ``PERSON`` is asked for."""
        if "PERSON" not in entities:
            return []
        return [
            RecognizerResult(
                entity_type="PERSON",
                start=match.start("name"),
                end=match.end("name"),
                score=HONORIFIC_SCORE,
                recognition_metadata={
                    RecognizerResult.RECOGNIZER_NAME_KEY: _RECOGNIZER_NAME,
                    RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                },
            )
            for match in self._pattern.finditer(text)
        ]


_TLD_EXTRACTOR: Final = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


class OfflineEmailRecognizer(EmailRecognizer):
    """Presidio's e-mail recognizer, with the domain checked against the public suffix list that
    tldextract bundles (never fetched, never cached)."""

    def validate_result(self, pattern_text: str) -> bool:
        return bool(_TLD_EXTRACTOR(pattern_text).fqdn)


def build_analyzer(models: Mapping[Language, Path]) -> AnalyzerEngine:
    """The engine for the languages of ``models``: each language's spaCy model directory (the
    one that holds ``config.cfg``). Loading takes seconds and 0.7 to 1.6 GB: once per child."""
    if not models:
        raise ValueError("at least one language model is needed")
    for name in ("presidio-analyzer", "presidio_analyzer", "tldextract"):
        logging.getLogger(name).setLevel(logging.WARNING)
    languages = sorted(models)
    codes: list[str] = list(languages)  # Presidio asks for list[str]
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": language, "model_name": str(models[language])}
                for language in languages
            ],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": MODEL_ENTITIES,
                "labels_to_ignore": IGNORED_LABELS,
            },
        }
    ).create_engine()
    registry = RecognizerRegistry(supported_languages=codes)
    for language in languages:
        registry.add_recognizer(
            SpacyRecognizer(
                supported_language=language,
                supported_entities=["PERSON", "LOCATION", "ORGANIZATION"],
            )
        )
        registry.add_recognizer(HonorificRecognizer(language))
        registry.add_recognizer(OfflineEmailRecognizer(supported_language=language))
        registry.add_recognizer(
            PhoneRecognizer(
                supported_language=language,
                supported_regions=PHONE_REGIONS,
                context=GERMAN_PHONE_CONTEXT if language == "de" else None,
            )
        )
        registry.add_recognizer(IbanRecognizer(supported_language=language))
        registry.add_recognizer(CreditCardRecognizer(supported_language=language))
        registry.add_recognizer(IpRecognizer(supported_language=language))
        registry.add_recognizer(UrlRecognizer(supported_language=language))
    return AnalyzerEngine(
        nlp_engine=nlp_engine,
        registry=registry,
        supported_languages=codes,
        log_decision_process=False,
    )
