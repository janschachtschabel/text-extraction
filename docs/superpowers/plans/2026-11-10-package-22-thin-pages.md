# Package 22: More Text From Thin Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pages of the WLO set that answer `content_too_short` although the material is there
(a collapsed solution, an attached worksheet, an edu-sharing node's file, an SVG's text, a
page's one video) give that material; and the unblock browser becomes a per-request choice
with its own concurrency limit.

**Architecture:** Two pure helpers in `convert/` reveal solution panels before hidden content
is dropped (`convert/panels.py`) and collect a page's attachment candidates from its links and
from Drupal JSON:API file entities in its scripts (`convert/attachments.py`). The pipeline
follows at most `ATTACHMENTS_MAX` candidates of a thin page through the normal fetch, compliance
and document steps and appends their text (`pipeline_attachments.py`). An edu-sharing render URL
is read through the repository's public REST API instead of its Angular shell
(`edusharing.py`); its file is the attachment. SVG becomes a document kind whose text is its
`<text>` elements. With `transcribe: true`, a thin page with exactly one embedded media file
hands that file to the existing transcription path. The request field `unblock` and the setting
`BROWSER_UNBLOCK_CONCURRENCY` close the package.

**Tech Stack:** Python 3.13, BeautifulSoup/lxml, defusedxml, httpx, MarkItDown, pytest.

**Spec:** Jan, 2026-11-10, thread "WLO-200-Fehlerseiten mit Prüfung aus untersuchen":
the wlo_200 run with site rules off (run 42705182964, v0.1.11) reached 89.5 %; of its 17
`content_too_short` pages, Jan's screenshots showed 12 as genuinely short and 5 as fixable
(serlo 11645 collapsed "Lösungsvorschlag", zebis Wasserkreislauf DOCX and Bruchrechnen XLS
attachments, schulcampus-rlp edu-sharing SVG node, aoe.fi 30-minute video). 09:50 Jan: "ok nimm
verbesserungen vor. eventuell für unblock option auswählbar machen wenn ein absturzrisiko
besteht - testen wir später nochmal. standardmäßig sollte es aber im modus ohne drin sein der
alles umgehen kann" (unblock stays on by default under `compliance: ignore`, but can be switched
off per request; the runner crash of run 42696935812 happened with four headful Chromes at once).

Evidence (saved on Jan's PC in `diag-p22/`, not committed; third-party content):
- serlo: the solution is server-rendered as
  `<nav><button>▾&nbsp;Lösungsvorschlag</button></nav><div class="serlo-solution-box hidden"><div class="hidden">…`;
  `convert_html` drops it (43 words, the task and the licence line only).
- zebis: the page's only file reference is in the Next.js flight payload
  (`self.__next_f.push([1,"…"])`) as a Drupal JSON:API entity
  `{"type":"file--file",…,"filename":"wasserkreislauf.docx","uri":{"value":"public://…","url":"/sites/default/files/2024-09/wasserkreislauf.docx"},"filemime":"application/vnd.openxmlformats-officedocument.wordprocessingml.document","filesize":162627,…}`;
  the rendered DOM shows a `<button>`, no link. `www.zebis.ch/sites/...` redirects to
  `api.zebis.ch`. The XLS is old binary Excel (MarkItDown's `xls` extra reads it).
- edu-sharing (`/edu-sharing/components/render/<uuid>`): the raw page is a 3.4 KB Angular shell;
  `GET /edu-sharing/rest/node/v1/nodes/-home-/<uuid>/metadata?propertyFilter=-all-`
  (`Accept: application/json`) answers 200 with `node.title`, `node.name`, `node.mimetype`,
  `node.mediatype`, `node.size`, `node.downloadUrl`
  (`/edu-sharing/eduservlet/download?nodeId=<uuid>`, public), `node.properties`
  (`cclom:title`, `cclom:general_description`, `cclom:general_keyword`, `ccm:wwwurl`,
  `ccm:commonlicense_key`, lists of strings). schulcampus-rlp's node is an Inkscape SVG
  (2 MB, two `<text>` elements: 21 words); twillo's is an MP3 (17 MB).
- aoe.fi: the material renders only under `https://aoe.fi/#/materiaali/1221`; it holds
  `<video controls><source src="/api/v1/download/…mp4" type="video/mp4">`.

## Global Constraints

- English in code, comments, developer docs, commit messages. User guide and UI texts change in
  English and German in the same commit (`tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`,
  `uv run python scripts/check_translation_pairs.py --base origin/main`).
- Every new setting is an env var with a documented default in `settings.py`, `.env.example`,
  `docs/configuration.md` and the Helm values (`tests/test_configuration_doc.py`,
  `tests/test_env_example.py`, `tests/test_defaults_parity.py`).
- Every extra request (attachment, REST metadata, node file) goes through the pipeline's own
  fetch: SSRF guard on the URL and each redirect, the request's compliance mode for the target's
  origin, the host's `DomainGate` turn and outbound limits, `DOCUMENT_MAX_BYTES`, and what is
  left of the request's deadline. Nothing bypasses rule 5 or 6.
- Result envelope stays as it is (rule 4); new warning codes are added to the warning catalogue
  with English and German UI texts.
- No new dependency (defusedxml, lxml, xlrd are already locked).
- Third-party HTML is never committed: tests use synthetic pages of the same shape.
- Version 0.1.12 in `pyproject.toml` and `CHANGELOG.md` (`[Unreleased]` until the release PR).

## Review Focus

1. A navigation dropdown or accordion menu whose panel is hidden with class `hidden` must stay
   dropped: only panels opened by a solution-like label are revealed (test in Task 1).
2. A thin page linking 20 PDFs fetches at most `ATTACHMENTS_MAX` (2) and never one over
   `DOCUMENT_MAX_BYTES` or on a host the request's compliance mode blocks (tests in Task 3).
3. An edu-sharing host whose REST API answers 401, 404, HTML or invalid JSON falls back to
   today's path for the render URL, with no error of its own (test in Task 4).
4. A malicious SVG (external entities, billion laughs, 50 MB) is refused by defusedxml or the
   size limit, never expanded (test in Task 5).
5. `unblock: false` from a user key, or with `compliance: report`, is accepted and changes
   nothing; `unblock: true` never unlocks the unblock browser for a user (test in Task 7).

---

### Task 1: Reveal solution panels

**Files:**
- Create: `src/text_extraction/convert/panels.py`
- Modify: `src/text_extraction/convert/markup.py` (`prepare_html`: call `reveal_solution_panels`
  before hidden elements are removed)
- Test: `tests/test_convert_panels.py`

**Interfaces:**
- Produces: `reveal_solution_panels(soup: BeautifulSoup) -> int` (number of panels revealed);
  `SOLUTION_LABEL: Final[re.Pattern[str]]`.

- [ ] **Step 1: Write the failing tests**

```python
SERLO_LIKE = """<html><body><main>
<p>Ein Werkstück hat die Masse m=657 g. Welche Dichte hat das Material?</p>
<nav><button><span>▾&nbsp;</span>Lösungsvorschlag</button></nav>
<div class="serlo-solution-box hidden"><div class="hidden">
<p>Mit diesem Ergebnis kann nun die Dichte bestimmt werden.</p></div></div>
</main></body></html>"""

def test_serlo_like_solution_survives_conversion():
    out = convert_html(SERLO_LIKE, "https://de.serlo.org/mathe/1/aufgabe", "markdown").content
    assert "Mit diesem Ergebnis kann nun die Dichte bestimmt werden." in out
    assert "Lösungsvorschlag" in out  # the label is kept as a heading before the panel

def test_details_solution_is_kept():
    html = "<main><p>Aufgabe.</p><details><summary>Lösung</summary><p>Zwölf.</p></details></main>"
    assert "Zwölf." in convert_html(html, "https://x.test/", "text").content

def test_hidden_menu_panel_stays_dropped():
    html = ('<main><p>Text.</p><div><button aria-controls="m">Menü</button></div>'
            '<ul id="m" class="hidden"><li>Impressum</li></ul></main>')
    assert "Impressum" not in convert_html(html, "https://x.test/", "text").content

def test_aria_controls_solution_panel_is_revealed():
    html = ('<main><p>Frage?</p><button aria-controls="s1">Show solution</button>'
            '<div id="s1" hidden><p>Forty-two.</p></div></main>')
    assert "Forty-two." in convert_html(html, "https://x.test/", "text").content
```

- [ ] **Step 2: Run** `uv run pytest tests/test_convert_panels.py -v`; expected: FAIL (the
  solution texts are missing).
- [ ] **Step 3: Implement `reveal_solution_panels`.** A panel is an element hidden by the
  `hidden` attribute, an inline `display:none`/`visibility:hidden` or the class token `hidden`,
  that is (a) the target of `aria-controls` of a `button`, `summary`, `[role=button]` or `a`, or
  (b) the next element sibling of such a control or of an element whose only control it is
  (serlo's `<nav><button>`), and whose control text (whitespace collapsed, leading arrows and
  `▾▸►▼+` stripped) matches `SOLUTION_LABEL`:
  `^(lösung(en|svorschlag|sweg)?|musterlösung|antwort(en)?|tipps?|hinweis(e)?|erklärung|solution(s)?|show (the )?solution|answer(s)?|show answer|hint(s)?)\b`
  (case-insensitive). Reveal the panel and every descendant hidden the same way (remove the
  attribute, the style declaration, the class token), insert `<p><strong>{label}</strong></p>`
  before the panel, and open every closed `<details>` whose `<summary>` matches the pattern.
- [ ] **Step 4: Run** the new tests and `uv run pytest tests/test_convert_html.py tests/test_convert_markup.py -q`; expected: PASS.
- [ ] **Step 5: Commit** `convert: reveal collapsed solution panels (package 22)`.

### Task 2: Attachment candidates of a page

**Files:**
- Create: `src/text_extraction/convert/attachments.py`
- Modify: `src/text_extraction/convert/html.py` (`_document_links` moves its logic into
  `link_attachments` and calls it; output unchanged)
- Test: `tests/test_convert_attachments.py`

**Interfaces:**
- Produces:
  `@dataclass(frozen=True) class Attachment: url: str; label: str; mime: str | None = None; size: int | None = None`;
  `attachment_candidates(html: str, url: str) -> list[Attachment]` (document links of the
  content first, in page order, then Drupal file entities, deduplicated by URL, http(s) only,
  resolved against `<base href>` or `url`).

- [ ] **Step 1: Failing tests:** a page with `<a href="/files/blatt.pdf">Arbeitsblatt</a>` in
  `<main>` and one in `<footer>` gives exactly the main one (label "Arbeitsblatt", absolute URL);
  a zebis-shaped page (synthetic: a `<script>self.__next_f.push([1,"…"])</script>` whose string
  holds an escaped `{"type":"file--file","filename":"wasserkreislauf.docx","uri":{"value":"public://x","url":"/sites/default/files/2024-09/wasserkreislauf.docx"},"filemime":"application/vnd.openxmlformats-officedocument.wordprocessingml.document","filesize":162627}`)
  gives `Attachment("https://www.zebis.ch/sites/default/files/2024-09/wasserkreislauf.docx", "wasserkreislauf.docx", "application/vnd…document", 162627)`;
  the same entity in a plain `application/json` script is found too; a `file--file` whose
  `uri.url` is `javascript:` or has user information is skipped; preview images
  (`filemime` `image/*`) are skipped.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement.** Scan each `<script>` text: for Next.js flight chunks, decode the
  JS string argument with `json.loads`; then find each `"type":"file--file"` and parse its
  enclosing object with `json.JSONDecoder().raw_decode` from the `{` that opens it; keep
  objects with `filename`, `uri.url` and a `filemime` that is not `image/*`. At most 20
  candidates per page.
- [ ] **Step 4: Run** the tests and `uv run pytest tests/test_convert_html.py -q`; PASS.
- [ ] **Step 5: Commit** `convert: attachment candidates from links and Drupal file entities`.

### Task 3: Follow the attachments of a thin page

**Files:**
- Create: `src/text_extraction/pipeline_attachments.py`
- Modify: `src/text_extraction/pipeline.py` (fast HTML result and rendered result, before the
  quality verdict of step 8), `src/text_extraction/settings.py`, `src/text_extraction/extract_models.py`
  (warning codes), `.env.example`, `docs/configuration.md`, `deploy/helm/text-extraction/values.yaml`,
  `docs/architecture.md` ("Extraction pipeline": new step 7b), UI warning texts in
  `ui/locales/en.json` and `de.json`
- Test: `tests/test_pipeline_attachments.py`

**Interfaces:**
- Consumes: `attachment_candidates` (Task 2); the pipeline's fetch and `DocumentSteps.extract`.
- Produces: settings `attachments_max: int = Field(default=2, ge=0, le=5)` (`ATTACHMENTS_MAX`,
  0 switches it off) and `attachments_below_words: int = Field(default=100, ge=0)`
  (`ATTACHMENTS_BELOW_WORDS`); warning codes `attachment_read` (one per attachment whose text
  was appended) and `attachment_failed` (fetched but not converted, blocked or over a limit).

- [ ] **Step 1: Failing tests** (fake fetcher as in `tests/test_pipeline_documents.py`): a page
  of 20 words linking `blatt.docx` answers with the page text, then `## blatt.docx` (Markdown;
  in `text` a line with the label, in `html` an `<h2>`) and the DOCX text, warning
  `attachment_read`, `status` `success` once the words reach `QUALITY_MIN_WORDS`; a page of 300
  words fetches nothing; a thin page with 20 PDF links fetches exactly 2; a candidate over
  `DOCUMENT_MAX_BYTES`, one whose host robots.txt disallows under `enforce`, and one that
  answers 404 each give `attachment_failed` and leave the page text as it was; an image or HTML
  candidate is not appended; `ATTACHMENTS_MAX=0` fetches nothing; an attachment that needs OCR
  appends its text layer only (no OCR hand-off).
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement** `async def append_attachments(run, result: ExtractResult, html: str) -> ExtractResult`
  in `pipeline_attachments.py`, called when `result.word_count < ATTACHMENTS_BELOW_WORDS`; each
  candidate goes through the same compliance check, host turn and fetch as the page (a redirect
  to another host takes that host's turn), is sniffed, and only MarkItDown kinds are converted;
  the deadline left is shared. `word_count` and the quality verdict are computed after the
  append. The cache key does not change (the setting is part of the deployment).
- [ ] **Step 4: Run** the new tests and `uv run pytest tests/test_pipeline*.py -q`; PASS.
- [ ] **Step 5: Commit** `pipeline: follow the attachments of a thin page (ATTACHMENTS_MAX)`.

### Task 4: edu-sharing render pages through the REST API

**Files:**
- Create: `src/text_extraction/edusharing.py`
- Modify: `src/text_extraction/pipeline.py` (before the page fetch of step 5, for a matching
  URL), `docs/architecture.md`
- Test: `tests/test_edusharing.py`

**Interfaces:**
- Produces: `render_node(url: str) -> tuple[str, str] | None` (base URL up to and including
  `/edu-sharing`, node id) for paths `/edu-sharing/components/render/<uuid>` with an optional
  version segment or query; `metadata_url(base: str, node: str) -> str`;
  `node_page(meta: dict[str, Any]) -> tuple[str, Attachment | None]` (a small HTML page:
  `<h1>` from `node.title`, else `cclom:title`, else `node.name`; the description, keywords and
  licence as paragraphs; `ccm:wwwurl` as a link; and the node's file as an `Attachment` from
  `downloadUrl`, `mimetype`, `size`, `name`).

- [ ] **Step 1: Failing tests:** `render_node` for the rlp and twillo URLs and `None` for
  `/edu-sharing/components/search`; `node_page` on a synthetic metadata JSON of the rlp shape
  (keywords Atommodell, Dalton, Chemische Reaktion; mimetype `image/svg+xml`) gives an `<h1>`
  "Zusammenfassung Kupfer mit Iod.svg", the keywords and the attachment; pipeline tests with a
  fake fetcher: the metadata answer plus an SVG file give `success`, `mode_used` `fast`, the
  SVG's text (Task 5) in `content` and no browser escalation; a metadata answer of 401, 404,
  `text/html` or broken JSON falls back to today's path; an MP3 node answers the metadata with
  the file listed (no transcription unless `transcribe: true`, which hands the file to the
  existing media path).
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement.** The metadata request is a normal fetch of the same origin with
  `Accept: application/json` (compliance of that URL, the host turn, `CONVERT_MAX_BYTES`); the
  node's file is followed as an attachment by Task 3's code regardless of the page's words (the
  file is the material).
- [ ] **Step 4: Run** tests and `uv run pytest tests/test_pipeline*.py -q`; PASS.
- [ ] **Step 5: Commit** `pipeline: read edu-sharing render pages through the REST API`.

### Task 5: SVG as a document kind

**Files:**
- Create: `src/text_extraction/documents/svg.py`
- Modify: `src/text_extraction/documents/sniff.py`, `src/text_extraction/documents/types.py`,
  `src/text_extraction/pipeline_documents.py` (route `svg` to it, in-process, no child)
- Test: `tests/test_document_svg.py`

**Interfaces:**
- Produces: `svg_text(data: bytes, max_chars: int) -> tuple[str | None, str]` (title from
  `<title>`, text: each `<text>`, `<flowRoot>` and `<desc>` as one paragraph in document order,
  `<tspan>` lines joined with a space).

- [ ] **Step 1: Failing tests:** a two-`<text>` SVG gives both sentences; an SVG with only
  paths gives "" (and the document answers `content_too_short` as usual); an SVG with a
  `<!DOCTYPE … [<!ENTITY …>]>` is refused (`conversion_failed`); `image/svg+xml` and a body
  starting with `<?xml …><svg` sniff as `svg`, while HTML stays HTML.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement** with `defusedxml.ElementTree.fromstring` (forbid DTD and entities)
  on bodies up to `DOCUMENT_MAX_BYTES`.
- [ ] **Step 4: Run** tests and `uv run pytest tests/test_document*.py -q`; PASS.
- [ ] **Step 5: Commit** `documents: SVG text (package 22)`.

### Task 6: Transcribe the one video of a thin page

**Files:**
- Modify: `src/text_extraction/pipeline_media.py` (`MediaSteps.tracks`), `docs/architecture.md`
  ("Audio and video": a new ruling M22 next to D4)
- Test: `tests/test_pipeline_media.py`

- [ ] **Step 1: Failing tests:** a page of 40 words with one `<video><source src="/a.mp4">`
  and `transcribe: true` becomes an `AsrNeeded` for `https://host/a.mp4` whose stand-in is the
  page's result and whose transcript is appended after the page text; the same page without
  `transcribe` keeps today's answer (embed listed); a page with two embeds, or one of 300
  words, keeps today's answer; a platform iframe (YouTube) as the one embed takes the platform
  path as a direct YouTube URL would.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement** in `MediaSteps.tracks`: when the request has `transcribe`, the page
  is below `ATTACHMENTS_BELOW_WORDS` and `page_media` found exactly one embed, run the embed
  through the media steps of a URL (compliance, host turn, `MEDIA_UPLOAD_MAX_BYTES`), and merge
  page title and text into the transcribed result.
- [ ] **Step 4: Run** tests and `uv run pytest tests/test_pipeline_media.py tests/test_media*.py -q`; PASS.
- [ ] **Step 5: Commit** `media: transcribe the one embedded video of a thin page`.

### Task 7: Unblock per request, and its own concurrency

**Files:**
- Modify: `src/text_extraction/extract_models.py`, `browser_models.py`, `job_models.py` (field
  `unblock`), `escalation.py` (`unblock_for(..., requested: bool = True)`), the callers in
  `pipeline.py` and `worker.py`, `settings.py` (`browser_unblock_concurrency`),
  `browser_worker.py` (the unblock pool's size), `ui/forms.py`, the form template, `ui/locales/en.json`
  and `de.json`, `mcp_server` tools that take `compliance`, `.env.example`, `docs/configuration.md`,
  Helm values, user guide en/de ("Site rules")
- Test: `tests/test_escalation.py`, `tests/test_extract_api.py`, `tests/test_browser_worker.py`,
  `tests/ui/test_forms.py` (or the existing access-offer test file)

**Interfaces:**
- Produces: request field `unblock: bool = Field(default=True, description="With compliance
  ignore (admins) and BROWSER_UNBLOCK_ENABLED: false renders with the standard browser instead
  of the unblock browser. Ignored otherwise.")`; setting
  `browser_unblock_concurrency: int = Field(default=2, ge=1)` (`BROWSER_UNBLOCK_CONCURRENCY`,
  capped at `BROWSER_CONCURRENCY`).

- [ ] **Step 1: Failing tests:** `unblock_for(admin, "ignore", enabled, requested=False)` is
  False and `requested=True` True; a user's `unblock: true` stays False; an admin's
  `compliance: ignore, unblock: false` job renders with the standard renderer (fake runner
  records which); the browser worker opens at most `BROWSER_UNBLOCK_CONCURRENCY` unblock pages
  at once while standard pages still use `BROWSER_CONCURRENCY`; the playground shows the
  checkbox "Unblock browser" / "Unblock-Browser" checked by default only when the offer has
  `unblock` and the step `ignore` is chosen, and an unchecked box sends `unblock: false`.
- [ ] **Step 2: Run** and see them fail.
- [ ] **Step 3: Implement**; `unblock` joins the cache key only where it changed the renderer.
- [ ] **Step 4: Run** `uv run pytest tests/test_escalation.py tests/test_extract_api.py tests/test_browser_worker.py tests/ui -q`; PASS.
- [ ] **Step 5: Commit** `unblock: per-request switch and BROWSER_UNBLOCK_CONCURRENCY`.

### Task 8: Docs, version, live check

**Files:** `docs/packages.md` (row 22), `CHANGELOG.md` (`[Unreleased]`: Added/Changed),
`pyproject.toml` stays 0.1.11 until the release PR, user guide en/de (what a thin page now gets),
`.github/workflows/live.yml` (site rules off passes `BROWSER_UNBLOCK_CONCURRENCY: "2"`).

- [ ] **Step 1:** Docs and changelog; `uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, `uv run python scripts/check_translation_pairs.py --base origin/main`.
- [ ] **Step 2:** Push; live runs `wlo_200` auto with `site_rules: off` and `check` on the branch.
  Done when both stay at or above 89.5 % and the not-ok tables no longer list serlo 11645 and
  the two zebis pages.
- [ ] **Step 3: Commit** `docs: package 22`.
