# PII corpus: sources and conventions

All fourteen texts were written for this purpose in October 2026 (8 German, 6 English). They are
original text released with the repository (Apache-2.0). Nothing is copied from a web page, a
book or a model's training corpus. No real person's data is in them.

## Invented data

- **Persons:** invented names. Where a name equals a public figure's it is a coincidence of a
  common name.
- **Places and institutions:** real names (`Bremen`, `Hamburg`, `Bundesregierung`, `Universität
  Bremen`, `University of Manchester`, ...) because teaching material mentions them. They are not
  personal data and are listed under `keep` in `gold.json` (decision D2: not found by default).
  `keep` also holds words that look like names but are not: `Pythagoras`, `Calvin`,
  `Goethestraße`, `Max-Planck-Gesellschaft`, `Humboldt-Universität`, `Energiewende`, and the
  Arabic, Cyrillic, Greek and Chinese words of the two multilingual texts (issue #14).
- **Addresses:** invented street numbers (`Beispielweg 12`, `14 Mill Lane`); the postcodes are
  plausible but not checked against a real address.
- **IBANs:** published test IBANs with valid checksums (`DE89 3704 0044 0532 0130 00`,
  `DE02 1203 0000 0000 2020 51`, `DE75 5121 0800 1245 1261 99`, `AT61 1904 3002 3457 3201`,
  `GB82 WEST 1234 5698 7654 32`, `NL91 ABNA 0417 1643 00`, `FR14 2004 1010 0505 0001 3M02 606`,
  `CH93 0076 2011 6238 5295 7`), written with and without spaces.
- **Cards:** the card networks' public test numbers (`4111 1111 1111 1111`,
  `5555 5555 5555 4444`, `3782 822463 10005`, `4012888888881881`, `378282246310005`).
- **Phone numbers:** fictional ranges where they exist: the Bundesnetzagentur's fictional blocks
  `030 23125-xxx`, `040 66969-xxx` and `0221 4710-xxx`; Ofcom's drama numbers `020 7946 0xxx`,
  `0113 496 0xxx`, `0141 496 0xxx`, `0117 496 0xxx` and `07700 900xxx`; US `555-01xx`. The other
  German numbers are invented and not reserved: the notations `0421 361-12345` and
  `+49 (0)30 1234567` that German pages use, `+49 441 99998-xxx`, and mobile numbers in the
  `0151`/`0171` ranges. UK mobile numbers `07911 123xxx` are plausible, not reserved.
- **E-mail addresses and URLs:** `example.org`, `example.com`, `example.net` only. Public suffix
  `.test` and `.example` are avoided in e-mail addresses because tldextract rejects unknown suffixes.
- **IP addresses:** documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`,
  `2001:db8::/32`).

## Files

| file | kind | language |
|---|---|---|
| `de_01_school_contact.md` | school contact page | German |
| `de_02_worksheet_credits.md` | teacher's worksheet with credits | German |
| `de_03_press_release.md` | press release | German |
| `de_04_forum_post.md` | forum post with replies | German |
| `de_05_cv_bio.md` | CV and short biography | German |
| `de_06_transcript.md` | podcast transcript snippet | German |
| `de_07_parent_letter.md` | letter to parents | German |
| `de_08_multilingual.md` | invitation with notes in other scripts (issue #14) | German |
| `en_01_school_contact.md` | school contact page | English |
| `en_02_worksheet_credits.md` | teacher's worksheet with credits | English |
| `en_03_press_release.md` | press release | English |
| `en_04_forum_post.md` | forum post with replies | English |
| `en_05_cv_bio.md` | CV and short biography | English |
| `en_06_multilingual.md` | invitation with notes in other scripts (issue #14) | English |

## `gold.json`

```json
{"<file>": {"spans": [{"type": "PERSON", "start": 0, "end": 6, "text": "Müller"}], "keep": ["Bremen"]}}
```

- `start` and `end` are Unicode code point offsets (Python string indices) into the exact file
  text, `end` exclusive; `text` equals `file_text[start:end]`.
- Types: `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`
  (the default types of decision D2) and `URL` (only when asked).
- A `PERSON` span is the name only, without salutation or title: `Herr Müller` is `Müller`,
  `Frau Dr. Schmidt` is `Schmidt`, `Prof. Dr. Henrik Sommer` is `Henrik Sommer`. Speaker labels
  and sign-offs that are names (`**Frank:**`, `Tobias`) are gold too. A user name that is not a
  name (`mathe_lehrerin_hb`) is not.
- `keep` lists words that must not be found as one of the default types. It is checked on every
  occurrence of the word in the file.
- Markdown markup (`**`, `-`, `#`) is part of the texts on purpose: extracted pages reach the
  PII step as Markdown.
