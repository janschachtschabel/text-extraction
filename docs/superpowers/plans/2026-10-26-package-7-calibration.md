# Package 7 calibration (Task 1)

Run on 2026-10-28 in the sandbox (4 vCPU, 15 GB RAM, Linux, Python 3.13.17). It measures the recognizer setup of N6 over the written-for-purpose corpus in `tests/pii/corpus/`, for both model sizes (D1) and three score thresholds. Task 2 takes `PII_SCORE_THRESHOLD` from here; Task 8's done-when test takes its recall pins from here.

## Summary

- **Chosen `PII_SCORE_THRESHOLD`: 0.4.** It is the score Presidio's `PhoneRecognizer` gives a number without context. At 0.5 the service would lose 6 of 16 German and 5 of 12 English phone numbers (those without a context word in front). 0.35 changes nothing against 0.4: no recognizer produced a score between 0.35 and 0.4 in this corpus.
- **Gate passed:** `lg` recall on German `PERSON` is 0.90 with NER only (52 of 58) and 0.97 with the honorific stand-in (56 of 58), so it is above 0.85. Task 4 can go ahead.
- **Pattern types are perfect:** e-mail, phone, IBAN, card and IP address reach precision 1.00 and recall 1.00 in German and English at 0.35 and 0.4, with `lg` and `md` alike. Phone drops at 0.5 (see above).
- **The honorific rule matters for German:** it lifts `lg` German `PERSON` recall from 0.90 to 0.97 (it finds `Müller`, `Neumann`, `Winter`, `Sommer` after `Herr` or `Frau`, which the NER model misses). English is already at 1.00 for `lg` without it; `md` English goes from 0.95 to 1.00.
- **Places and institutions stay out by default (D2):** no keep entry that the run checked (places and institutions) is found under the default types except the two persons' names below. The NER does find 36 of 47 German and 34 of 36 English keep-word occurrences as `LOCATION` or `ORGANIZATION` when those are asked for, which is why D2 (a) is right. The only default-type keep hits are `Pythagoras` and `Calvin`, a NER `PERSON` on a person's name used as a concept: `lg` hit `Pythagoras` in German and `Calvin` in English; `md` also hit `Pythagoras` in English.
- On this corpus `lg` and `md` are close on recall (German `PERSON` 0.97 and 0.97 with the stand-in, 0.90 and 0.91 without it); `lg` is better on precision (0.93 against 0.86 for German `PERSON`, with 4 and 9 false positives) and needs 1.6 GB against 0.7 GB. The corpus is small (58 German, 39 English `PERSON` spans); one span is 1.7 points, so do not read the recall difference as more than noise.

## Setup

- Scratch venv `uv venv -p 3.13`, `uv pip install presidio-analyzer==2.2.364 spacy==3.8.16`. Resolved on the way: `phonenumbers 9.0.42`, `tldextract 5.4.0`.
- The four model wheels were downloaded from the `explosion/spacy-models` GitHub releases, unpacked with `python -m zipfile -e`, and loaded by path through Presidio's `NlpEngineProvider` (`model_name` = the inner directory `<name>/<name>-3.8.0`). Each wheel's `meta.json` says `spacy_version >=3.8.0,<3.9.0` and licence MIT.
- Recognizers as N6 (one `AnalyzerEngine`, `de` and `en`): `SpacyRecognizer` with the mapping `PER`, `PERSON` to `PERSON`, `LOC`, `GPE` to `LOCATION`, `ORG` to `ORGANIZATION` and all other model labels ignored (`MISC`, `DATE`, `TIME`, `NORP`, `MONEY`, `CARDINAL`, `ORDINAL`, `QUANTITY`, `PERCENT`, `EVENT`, `FAC`, `LAW`, `LANGUAGE`, `PRODUCT`, `WORK_OF_ART`); `EmailRecognizer` (offline `tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)`, no network access and no traceback in the logs), `PhoneRecognizer` (regions `DE`, `AT`, `CH`, `GB`, `US`; German context words `telefon`, `tel`, `telefonisch`, `fon`, `mobil`, `handy`, `fax`, `rufnummer`, `erreichbar` for `de`, Presidio's English defaults for `en`), `IbanRecognizer`, `CreditCardRecognizer`, `IpRecognizer`, `UrlRecognizer`. Context enhancement is Presidio's default (`LemmaContextAwareEnhancer`, +0.35).
- N7 as written: the analysed entities are the six default types of D2 (`URL`, `LOCATION`, `ORGANIZATION` in separate runs, because asking for them changes the overlaps), spans at or above the threshold, overlap rule (inside a span of equal or higher score is dropped; partial overlap cuts the lower span, dropped when nothing with a letter or digit is left), segments of at most 20,000 characters (all files are shorter, so one segment each).
- Honorific stand-in (the `HonorificRecognizer` is built in Task 4): a regex, score 0.7, span is the name only (one or two capitalised words, hyphens allowed). `spec` follows the plan literally: a name after `Herr`, `Frau`, `Hr.`, `Fr.` (de) or `Mr`, `Mrs`, `Ms`, `Mx`, `Dr` with optional dot (en), with up to two `Dr.`/`Prof.` between. The `herrn` variant also accepts the German `Herrn` (see the findings). Tables show "NER + patterns" (no stand-in) and "+ honorific" (`spec`).
- Matching against `gold.json` (Unicode code point offsets, verified: every gold `text` equals the slice of the file): a gold span is **found** when predictions of its type cover at least 80 % of its characters; a prediction is **correct** when it overlaps a gold span of its type. These are the masking-oriented rules: `Herrn Prof. Hoffmann` for gold `Hoffmann` still masks the name. A strict one-to-one match (IoU at least 0.5) is given for `PERSON` below. Keep words are checked on every occurrence of the word in its file: a hit is a predicted default-type span that overlaps it.
- Time and memory: one process per size and threshold. Load is the time to build the engine (the first run of each size found the model files cold: 8.8 s for `lg`, otherwise 3.3 to 3.9 s for `lg` and 2.2 to 2.7 s for `md`). Analysis is the sum over the 12 files (3,824 words, 26,232 characters) after one warm-up call per language, about 6,400 words/s, as in D1. Peak RSS is `ru_maxrss` after loading both languages and analysing: 1.6 GB for `lg`, 0.68 GB for `md` (D1: 1.6 GB and 0.7 GB).
- The scripts (`calibrate.py`, `build_corpus.py`, `make_tables.py`) live in `.superpowers/sdd/2026-10-26-package-7-pii/` and are not committed (ledger ruling R4). Commands: `python calibrate.py run --size lg --threshold 0.4 --out run-lg-0.4.json` for each size and threshold, `python calibrate.py probe --size lg --out probe-lg.json` for the context experiment. The scratch venv, wheels and unpacked models were deleted afterwards.

### Model wheels (pins for the manifest, Task 3)

All four are `3.8.0`, `py3-none-any`, from `https://github.com/explosion/spacy-models/releases/download/<name>-3.8.0/<name>-3.8.0-py3-none-any.whl`, downloaded 2026-10-28. The sizes and hashes equal the values measured on 2026-10-26.

| model | URL | size (bytes) | unpacked | SHA-256 |
|---|---|---|---|---|
| `de_core_news_lg` | `https://github.com/explosion/spacy-models/releases/download/de_core_news_lg-3.8.0/de_core_news_lg-3.8.0-py3-none-any.whl` | 567,843,151 | 610 MB | `36fda650e476b54d5e87803635e36dadd1e8e034c4b5962088586d684f4c9fed` |
| `en_core_web_lg` | `https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl` | 400,658,291 | 425 MB | `293e9547a655b25499198ab15a525b05b9407a75f10255e405e8c3854329ab63` |
| `de_core_news_md` | `https://github.com/explosion/spacy-models/releases/download/de_core_news_md-3.8.0/de_core_news_md-3.8.0-py3-none-any.whl` | 44,398,316 | 60 MB | `b903f59220f1e76dd672acdaa7fa454d6703fe056c5ccd6457820e70874116d0` |
| `en_core_web_md` | `https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl` | 33,480,380 | 55 MB | `5e6329fe3fecedb1d1a02c3ea2172ee0fede6cea6e4aefb6a02d832dba78a310` |

## Corpus

12 files written for the purpose (see `tests/pii/corpus/SOURCES.md`): 7 German (240 to 352 words by whitespace count, 2,148 in all) and 5 English (273 to 363 words, 1,676 in all): school contact page, worksheet credits, press release, forum post, CV and bio, and, for German, a podcast transcript and a letter to parents.

| language | PERSON | PHONE_NUMBER | EMAIL_ADDRESS | IBAN_CODE | CREDIT_CARD | IP_ADDRESS | URL (asked only) | keep entries |
|---|---|---|---|---|---|---|---|---|
| de | 58 | 16 | 11 | 5 | 4 | 4 | 5 | 41 entries, 47 occurrences |
| en | 39 | 12 | 8 | 4 | 3 | 5 | 4 | 30 entries, 36 occurrences |

The corpus was corrected twice after the first calibration run, in each case because the run exposed a mistake in the gold file, not to fit a model: unmarked names (greetings and sign-offs such as `Hallo Sabine`, speaker labels such as `**Frank:**`) were added as gold, a title that had slipped into a gold span was moved out, `Pythagoras` and `Calvin` became keep words, and the English mobile number `+44 7700 900987` was replaced by `+44 7911 123456` (see the findings).

## Results

Cells read "precision / recall (found of gold)". The columns of each table: `de`/`en` with and without the honorific stand-in. Rows of `URL`, `LOCATION`/`ORGANIZATION` are for requests that ask for them (not default, D2) and do not depend on the stand-in.

#### `lg`, threshold 0.35

Model load 8.84 s, analysis of 3824 words in 12 files 0.628 s, peak RSS 1603 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.93 / 0.90 (52/58) | 0.93 / 0.97 (56/58) | 0.97 / 1.00 (39/39) | 0.97 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (12/12) | 1.00 / 1.00 (12/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 1 of 36 (Calvin) | 1 of 36 (Calvin) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 34 of 36 | same |

#### `lg`, threshold 0.4

Model load 3.85 s, analysis of 3824 words in 12 files 0.586 s, peak RSS 1603 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.93 / 0.90 (52/58) | 0.93 / 0.97 (56/58) | 0.97 / 1.00 (39/39) | 0.97 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (12/12) | 1.00 / 1.00 (12/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 1 of 36 (Calvin) | 1 of 36 (Calvin) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 34 of 36 | same |

#### `lg`, threshold 0.5

Model load 3.35 s, analysis of 3824 words in 12 files 0.625 s, peak RSS 1603 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.93 / 0.90 (52/58) | 0.93 / 0.97 (56/58) | 0.97 / 1.00 (39/39) | 0.97 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 0.62 (10/16) | 1.00 / 0.62 (10/16) | 1.00 / 0.58 (7/12) | 1.00 / 0.58 (7/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 1 of 36 (Calvin) | 1 of 36 (Calvin) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 34 of 36 | same |

#### `md`, threshold 0.35

Model load 2.63 s, analysis of 3824 words in 12 files 0.541 s, peak RSS 675 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.85 / 0.91 (53/58) | 0.86 / 0.97 (56/58) | 0.93 / 0.95 (37/39) | 0.93 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (12/12) | 1.00 / 1.00 (12/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 2 of 36 (Calvin, Pythagoras) | 2 of 36 (Calvin, Pythagoras) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 32 of 36 | same |

#### `md`, threshold 0.4

Model load 2.73 s, analysis of 3824 words in 12 files 0.602 s, peak RSS 676 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.85 / 0.91 (53/58) | 0.86 / 0.97 (56/58) | 0.93 / 0.95 (37/39) | 0.93 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (16/16) | 1.00 / 1.00 (12/12) | 1.00 / 1.00 (12/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 2 of 36 (Calvin, Pythagoras) | 2 of 36 (Calvin, Pythagoras) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 32 of 36 | same |

#### `md`, threshold 0.5

Model load 2.64 s, analysis of 3824 words in 12 files 0.595 s, peak RSS 676 MB.

| type | de NER + patterns | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| PERSON | 0.85 / 0.91 (53/58) | 0.86 / 0.97 (56/58) | 0.93 / 0.95 (37/39) | 0.93 / 1.00 (39/39) |
| EMAIL_ADDRESS | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (11/11) | 1.00 / 1.00 (8/8) | 1.00 / 1.00 (8/8) |
| PHONE_NUMBER | 1.00 / 0.62 (10/16) | 1.00 / 0.62 (10/16) | 1.00 / 0.58 (7/12) | 1.00 / 0.58 (7/12) |
| IBAN_CODE | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) |
| CREDIT_CARD | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (3/3) | 1.00 / 1.00 (3/3) |
| IP_ADDRESS | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (4/4) | 1.00 / 1.00 (5/5) | 1.00 / 1.00 (5/5) |
| keep-word hits (occurrences) | 1 of 47 (Pythagoras) | 1 of 47 (Pythagoras) | 2 of 36 (Calvin, Pythagoras) | 2 of 36 (Calvin, Pythagoras) |
| URL (asked only), de / en | 1.00 / 0.80 (4/5) | same | 1.00 / 1.00 (4/4) | same |
| keep words found as LOCATION or ORGANIZATION (asked only) | 36 of 47 | same | 32 of 36 | same |

### `PERSON` with a strict one-to-one match (IoU at least 0.5), threshold 0.4

| size | de NER + patterns (P / R) | de + honorific | en NER + patterns | en + honorific |
|---|---|---|---|---|
| `lg` | 0.91 / 0.88 (51/58) | 0.92 / 0.95 (55/58) | 0.97 / 1.00 (39/39) | 0.97 / 1.00 (39/39) |
| `md` | 0.79 / 0.84 (49/58) | 0.80 / 0.90 (52/58) | 0.93 / 0.95 (37/39) | 0.93 / 1.00 (39/39) |

## Chosen `PII_SCORE_THRESHOLD`: 0.4

- Presidio's `PhoneRecognizer` scores every number 0.4 and lifts it to 0.75 (+0.35) when a context word is among the five content words before it (Presidio's `LemmaContextAwareEnhancer` counts lemmas without stop words and punctuation, so `Uhr unter` in `Telefonisch erreichen Sie ... 13:00 Uhr unter` does not hide `telefonisch`). A threshold above 0.4 therefore silently drops every phone number without a context word. Measured with `lg`: at 0.5, German phone recall falls from 1.00 to 0.62 (10 of 16) and English from 1.00 to 0.58 (7 of 12); at 0.4 and 0.35 it is 1.00.
- No score between 0.35 and 0.4 occurs. The score distribution (raw recognizer spans before N7 overlap resolution, so e-mail address fragments count as `URL` spans) over all 12 files at threshold 0 is: `EMAIL_ADDRESS` 1.0 (19 spans), `IBAN_CODE` 1.0 (9), `CREDIT_CARD` 1.0 (8), `PERSON` 0.85 (100, the NER default), `PHONE_NUMBER` 0.4 (12) and 0.75 (17), `IP_ADDRESS` 0.6 (9), `URL` 0.5 (24), 0.6 (7), 0.95 (2). So 0.35 only adds risk for other inputs and nothing here; 0.4 is the lowest value that keeps the weakest legitimate pattern. The `URL` spans at 0.5 are the fragments inside e-mail addresses; they are asked-only and N7 resolves them against the e-mail span.
- `PERSON` is unaffected by the threshold in this range (all NER spans score 0.85), so the threshold is in effect the phone-number floor, as the plan expected.

### German context words

Probe: the 16 German and 12 English gold phone numbers scored at threshold 0 with `lg`, with the N6 German context words and with Presidio's English defaults only.

| language | with N6 context words | with the English defaults only |
|---|---|---|
| de (16 numbers) | 10 lifted to 0.75, 6 stay at 0.4 | 0 lifted, all 0.4 |
| en (12 numbers) | 7 lifted to 0.75, 5 stay at 0.4 (English defaults) | same |

- The German words work when they stand right before the number: `Telefon:` and `Mobil:` (also with the Markdown bold markers around the label), `Fax lautet`, `Rufnummer`, `telefonisch unter`, `erreichbar`.
- They do not lift: `unter der Nummer`, `Durchwahl lautet`, `Festnetz:`, `Sie ist unter`, `entweder im Sekretariat unter`, and `man sie unter ... erreichen kann`, where the verb comes after the number (Presidio looks only at words before it). This costs nothing at threshold 0.4 and a lot at 0.5.
- For Task 4 (optional, none needed for the pins): consider adding `durchwahl`, `festnetz`, `nummer`, `anrufen`, `erreichen`, `kontakt` to the German context words. The English defaults (`phone`, `number`, `mobile`, `call`, ...) lift 7 of the 12 English numbers; the other 5 are introduced by `main office on`, `can be reached on` (twice), `landline:` and `department office on`.

## Numbers that Task 8 pins

`lg`, threshold 0.4, with the honorific stand-in (`spec`), matching as defined in Setup (a gold span is found when 80 % of it is covered). Rounded down to two decimals:

| type | de | en |
|---|---|---|
| `PERSON` | **0.96** (56 of 58; strict IoU matching: 0.94, 55 of 58) | **1.00** (39 of 39) |
| `EMAIL_ADDRESS` | 1.00 (11 of 11) | 1.00 (8 of 8) |
| `PHONE_NUMBER` | 1.00 (16 of 16) | 1.00 (12 of 12) |
| `IBAN_CODE` | 1.00 (5 of 5) | 1.00 (4 of 4) |
| `CREDIT_CARD` | 1.00 (4 of 4) | 1.00 (3 of 3) |
| `IP_ADDRESS` | 1.00 (4 of 4) | 1.00 (5 of 5) |

- Pattern types are pinned at 1.0, `PERSON` at the numbers above. The two German `PERSON` misses with the stand-in are `Sabine` (a lone first name in a greeting, `Hallo Sabine,`) and `Henrik Sommer` (after `Prof. Dr.`, no `Herr` or `Frau`). If the Task 8 test compares exact or IoU-matched spans instead of coverage, it should pin the strict column (0.94 for German `PERSON`).
- Precision pins are not needed by the plan; for reference `lg` German `PERSON` precision is 0.93 and the keep words found by default are `Pythagoras` and `Calvin`.
- Keep words (D2): the run checked the keep lists as they stood then (41 German and 30 English entries, almost all places and institutions; of the capitalised non-name nouns only `Energiewende` in `de_01`). It found no place or institution under the default types; the two hits above are NER false positives on persons' names used as concepts. The keep lists were extended afterwards, without re-measuring, with the capitalised non-name nouns that occur (`Energiewende`, `Photosynthese`, `Klimawandel`, `Chlorophyll`, `Glukose`, `Kohlenstoffdioxid`, `Landwirtschaft`, `Mobilitätswende`, `Bruchrechnung`, `Klimabildung`, English `Photosynthesis`, `Energy Transition`) in every file where they occur; Task 8's corpus test checks the extended lists.

## Findings and surprises

1. **`Herrn` is missing from the plan's German honorific list.** In running German text the dative and accusative is the usual form (`mit Herrn Dr. Hartmann`, `von Herrn Prof. Hoffmann`, `Herrn Müller`). The plan lists `Herr`, `Frau`, `Hr.`, `Fr.`. In this corpus the NER found those names anyway, so adding `Herrn?` changed recall by no span (56 of 58 either way), but it adds a false positive when a title word follows (`Herrn Doktor Lukas Frank` took `Doktor` as the name). Recommendation for Task 4: accept `Herrn`, and treat `Doktor` as a title word between salutation and name. The rule gets `Herr` and `Frau` right where the NER misses the name: `Müller`, `Neumann`, `Winter`, `Sommer`.
2. **Titles without a salutation are not covered.** `Prof. Dr. Henrik Sommer` (after `von`) is missed by `lg` and by `md`. A rule on `Dr.` or `Prof.` directly before a capitalised name would find it.
3. **E-mail addresses with an unknown suffix are not found.** Presidio's `EmailRecognizer` validates with `tldextract` and the bundled list: `a@schule.test` and `a@x.example` give an empty `fqdn` and are dropped, `a@x.example.org` is found. This also applies to intranet names such as `.local`. Test data for Task 4 and Task 8 must use `example.org`, `.com` or `.net` for e-mail addresses (not `.test`, `.example`, `.invalid`).
4. **The UK drama range `07700 900xxx` is invalid for `libphonenumber`.** `+44 7700 900987` was missed by every region; `07700 900123` was found only because the German region accepts the digits as a German number. Phone numbers must be valid per the library's metadata (leniency 1 in Presidio), so fictional ranges are not always found. The corpus keeps `07700 900555` (found via the German region) and uses `07911 123xxx` elsewhere.
5. **The NER labels URLs, user names and markup as `PERSON` now and then.** In `lg`: the whole URL `https://www.beispiel-lernen.example.org/material/photosynthese`, the forum user name `mathe_lehrerin_hb`, and the span `:** Vielen Dank` from Markdown bold markers (`md` adds `Telefonisch`, `Herzlich`, `Schreib`, `Lindenallee`, `Herrn Doktor`). With `PERSON` at 0.85 the URL span also hides the URL (asked-only type, score 0.5 to 0.95) when `URL` and `PERSON` are requested together: German URL recall 0.80 in that run. N7's rule behaves as written; the cause is the NER span.
6. **`md` vs `lg`:** Speaker labels (`**Frank:**`) and `Lukas Frank` were handled differently by the two models (`lg` misses `Sabine`, `md` misses `Lukas Frank` and, without the stand-in, `Winters` and `Summers` in English); on this corpus recall is a tie, `lg` has fewer false positives (4 against 9 for German `PERSON`, 1 against 3 for English). D1 (a) stays, but this corpus does not show a recall gain of `lg` over `md`; D1's observation that `md` missed `Dr. Anna Schmidt` and `lg` found her was not repeated here as such.
7. **Keep-word false positives by the NER:** `Pythagoras` (both models in German, only `md` in English) and `Calvin` (`Calvin cycle`; English only, both models). They are not places or institutions. Nothing to do for the default set, but a teacher's worksheet with historic names will have a few such false positives.
8. **Speed and memory** match D1: both languages loaded, `lg` 1.6 GB and `md` 0.68 GB RSS; about 6,400 words/s.
9. Presidio's own loggers were at `WARNING` and nothing was logged during the runs; the offline `tldextract` made no network request.
