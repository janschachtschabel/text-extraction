"""The Presidio engine of the PII children (ruling N6). Imported in the children only: it brings
in Presidio, spaCy and tldextract, which the API and the parent of the NLP worker never load.

One ``AnalyzerEngine`` serves ``de`` and ``en``:

- **Names, places, organisations** come from the spaCy models through Presidio's
  ``SpacyRecognizer``: ``PER`` and ``PERSON`` are ``PERSON``, ``LOC`` and ``GPE`` are
  ``LOCATION``, ``ORG`` is ``ORGANIZATION``. Every other label is ignored (the German models
  have ``MISC``, the English ones dates, money and more). ``LatinScriptSpacyRecognizer`` drops
  a span that has letters but none of the Latin script (issue #14): the German and English
  models were not trained on Arabic, Cyrillic, Greek or CJK text and tag its words at random
  (``использования в`` became ``[PERSON]``). A span with one Latin letter stays (``Herr Иванов``),
  and the honorific rule below still finds a name of any script after a salutation.
- **Patterns** per language: e-mail address, phone number (regions ``DE``, ``AT``, ``CH``,
  ``GB``, ``US``; German context words that lift a number's score from 0.4 to 0.75), IBAN,
  credit card, IP address and URL.
- **Honorifics** (``HonorificRecognizer``): a name after ``Herr``, ``Herrn``, ``Frau``, ``Hr.``,
  ``Fr.`` (German) or ``Mr``, ``Mrs``, ``Ms``, ``Mx``, ``Dr`` (English), after a title alone
  (``Prof.``, ``Dr.``, ``Prof. Dr.``, ``Doktor``), or after a salutation and a title, is a
  ``PERSON`` of score 0.7, and the span is the name only. The NER usually takes a salutation and
  its name as one ``PERSON`` (``Herr Müller`` becomes ``[PERSON]``, which wins on its higher
  score); this rule covers the names the NER misses after a salutation, which then become
  ``Herr [PERSON]``. A name word is an upper-case letter and letters of any script, with internal
  capitals and an apostrophe (``McDonald``, ``O'Brien``, ``Nowak-Šimić``), and the name ends
  where its word does. ``Fr.`` that closes a range of weekdays (``Mo.-Fr.``, ``Mo. bis Fr.``) is
  Friday, not ``Frau``.

tldextract never goes online: Presidio's e-mail recognizer validates the domain with it, and
tldextract's default would fetch the public suffix list from publicsuffix.org (and cache it
on disk). ``OfflineEmailRecognizer`` uses ``TLDExtract(suffix_list_urls=(), cache_dir=None)``:
the list bundled with the package, no network, no file. Presidio's and tldextract's loggers are
set to ``WARNING``; the decision log is off.
"""

import logging
import re
import sys
import unicodedata
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


def _upper_case_letters() -> str:
    """Every upper-case character of Unicode, as the ranges of a character class: ``re`` has no
    ``\\p{Lu}``, and a name part starts with one (``Šimić``, ``Łukasiewicz``, ``Ørsted``)."""
    ranges: list[tuple[int, int]] = []
    for code in range(sys.maxunicode + 1):
        if chr(code).isupper():
            if ranges and ranges[-1][1] == code - 1:
                ranges[-1] = (ranges[-1][0], code)
            else:
                ranges.append((code, code))
    return "".join(
        f"\\U{first:08x}" if first == last else f"\\U{first:08x}-\\U{last:08x}"
        for first, last in ranges
    )


_UPPER = _upper_case_letters()
_REST = rf"[^\W\d_{_UPPER}]"  # a letter that is not upper case (lower case, or caseless)
_APOSTROPHE = "['\u2019]"
# Words that are titles or degrees, never a name: a name part must not be one of them, or the title
# group would give back its last word to the name ("Prof. Dr. med. Meier" must not yield "Dr").
_NOT_A_NAME = r"(?!(?:Dr|Prof|Doktor|Ing|Phil|Med|Jur|Dent|Habil)\b)"
# One word of a name: an upper-case letter, then letters that are not, with internal capitals and
# apostrophes before one (McDonald, O'Brien, Dell'Orto). Each repetition starts with an apostrophe
# or a capital, which the letters before it are not: one way to match, so linear time.
_WORD = rf"[{_UPPER}](?:{_REST}+|{_APOSTROPHE}(?=[{_UPPER}]))(?:{_APOSTROPHE}?[{_UPPER}]{_REST}+)*"
_NAME_PART = rf"{_NOT_A_NAME}{_WORD}(?:-{_NOT_A_NAME}{_WORD})*"  # Meyer-Schulz is one part
# One or two words; the name ends where the word does (never "Mc" of "McDonald", never "Dell" of
# "Dell'orto"), but a possessive "'s" or a trailing apostrophe is no part of it ("Smith's").
_NAME = (
    rf"(?P<name>{_NAME_PART}(?:[ \t\xa0]+{_NAME_PART})?)"
    rf"(?!\w|{_APOSTROPHE}(?!s\b)[^\W\d_])"
)
_GAP = r"[ \t\xa0]+"
# Titles that may stand between a salutation and the name, or alone before it: Dr., Prof. or
# Doktor, then any number of titles and academic degrees (med., dent., rer. nat., h. c., Ing., ...).
_TITLE = r"(?:Dr|Prof)\.|Doktor"
_DEGREE = (
    r"(?:med|dent|vet|rer|nat|oec|pol|phil|jur|iur|theol|habil|mult|techn|agr)\."
    r"|h\.[ \t\xa0]?c\."
    r"|(?:Ing|Phil|Med|Jur|Dent|Habil)\.?"
)
# The chain is bounded (the longest real one is "Prof. Dr. Dr. h. c. mult."): on page text of
# "Dr. Dr. Dr. ..." an open-ended chain would backtrack quadratically. A bare degree word can still
# be taken as a title before a first name ("Dr. Phil Collins" gives "Collins"): a known trade-off.
_TITLES = rf"(?:{_TITLE}){_GAP}(?:(?:{_TITLE}|{_DEGREE}){_GAP}){{0,6}}"
# "Fr." that closes a range of weekdays ("Mo.-Fr.", "Mo. bis Fr.") is Friday, not Frau.
_NOT_FRIDAY = (
    r"(?<!(?:Mo|Di|Mi|Do)[-\u2013])"
    r"(?<!(?:Mo|Di|Mi|Do)\.[-\u2013])"
    r"(?<!(?:Mo|Di|Mi|Do)\.[ \xa0][-\u2013][ \xa0])"
    r"(?<!(?:Mo|Di|Mi|Do)[ \xa0]bis[ \xa0])"
    r"(?<!(?:Mo|Di|Mi|Do)\.[ \xa0]bis[ \xa0])"
)
_SALUTATIONS: Final = {
    "de": rf"(?:Herrn?|Frau|Hr\.|{_NOT_FRIDAY}Fr\.)",
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


def has_latin_letter_or_none(text: str) -> bool:
    """Whether ``text`` has a letter of the Latin script, or no letter at all. Marks and
    punctuation are no letters: ``مُخصص`` has Arabic letters only."""
    letters = False
    for char in text:
        if char.isalpha():
            if unicodedata.name(char, "").startswith("LATIN "):
                return True
            letters = True
    return not letters


class LatinScriptSpacyRecognizer(SpacyRecognizer):
    """Presidio's spaCy recognizer without the spans of letters outside the Latin script: the
    German and English models only guess on them."""

    def analyze(
        self,
        text: str,
        entities: list[str],
        nlp_artifacts: NlpArtifacts | None = None,
    ) -> list[RecognizerResult]:
        return [
            result
            for result in super().analyze(text, entities, nlp_artifacts)
            if has_latin_letter_or_none(text[result.start : result.end])
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
            LatinScriptSpacyRecognizer(
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
