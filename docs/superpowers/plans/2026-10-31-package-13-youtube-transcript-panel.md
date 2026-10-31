# Package 13: YouTube transcript panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When youtube-transcript-api and yt-dlp give no readable track for a YouTube video in an `auto` request, the browser worker opens the watch page and reads YouTube's new transcript view, so the answer carries the transcript instead of the metadata only.

**Architecture:** The fast result of such a video (`transcript_source: metadata_only` with `subtitles_blocked` or `subtitles_failed`) escalates like a page that needs the browser (B5). The browser worker renders the canonical watch page with a new render option, `transcript_panel`. After the page has loaded and the consent dialog is declined, the render reads the transcript only where YouTube serves its new view (`PAmodern_transcript_view`): it opens the description, presses "Show transcript" and keeps the body of the page's own `POST /youtubei/v1/get_panel` answer. The pipeline parses that body into cues and builds the transcript from the draft's details. Without the new view, or when anything fails, the draft stays the answer, with a warning. The page's own text is never the answer.

**Tech Stack:** Python 3.13, FastAPI, Playwright Chromium (standard browser mode, not unblock), pydantic, pytest and pytest-httpserver, existing `media/transcript.py` (`Cue`, `build_transcript`).

**Spec:** Jan's request of 2026-10-31 21:33 UTC and his choice at 23:11 UTC ("Fallback nur neues Panel bauen"), the spike `docs/superpowers/plans/2026-10-31-youtube-transcript-panel-spike.md`, and `docs/architecture.md` "Audio and video" (D1, M1-M6).

## Global Constraints

- CLAUDE.md applies in full. Code, comments, developer docs and API texts are in English. User-facing docs and UI texts are in English and German, changed in the same pull request (`tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`, `scripts/check_translation_pairs.py`).
- Rule 6: no getting past YouTube's bot protection. The render uses the standard browser (honest user agent, `navigator.webdriver` as Chromium sets it), never unblock mode or Patchright for this, sends no request of its own to YouTube's APIs (only the page's own requests, started by a click) and never reads or forges an attestation. The classic panel (`get_transcript`) is not used.
- Rule 5: the render goes through the SSRF egress proxy and the navigation checks like any render (B2); the `get_panel` body is read at most up to `MEDIA_SUBTITLE_MAX_BYTES` (5,000,000).
- Rule 2: the new setting is an env var with a documented default, in `docs/configuration.md`, the Helm chart and `deploy/helm/test.sh` where settings are checked.
- No upstream proxy (Jan, 2026-10-31). No new dependency.
- Logs carry no URL, video id, cookie or transcript text: only event names, outcomes and counts.
- Commit messages end with the session trailer; no model identifiers, no team names in any deliverable.

## Decisions for Jan (recommended = a)

- **D1, `transcript_source` of a panel transcript.** (a) a new value `transcript_panel`: the new view does not say whether the track is a person's or automatic, so neither existing value would be honest. (b) always `auto_subtitles`.
- **D2, when the panel is tried.** (a) only for an `auto` request with `transcribe: false` whose fast result is a YouTube video's `metadata_only` answer carrying `subtitles_blocked` or `subtitles_failed` (the track was there but could not be read), never when YouTube said the video has no subtitles; `mode: browser` keeps rendering the page as a page. (b) also for every `mode: browser` request of a YouTube URL.
- **D3, the switch.** (a) `MEDIA_TRANSCRIPT_PANEL_ENABLED`, default `true`; it does nothing without `BROWSER_ENABLED`. (b) default `false`.

## Rulings (P13-S*)

- **P13-S1, trigger.** `escalation.needs_transcript_panel(envelope) -> bool` is true when the result's `mode_used` is `fast`, `media.platform` is `youtube`, `media.transcript_source` is `metadata_only`, `cache` is not `hit` or `revalidated`, and `warnings` contains `subtitles_blocked` or `subtitles_failed`. Every place that asks `needs_browser` (`extract_api.py`, `worker.py`, `pipeline.py`) escalates when `needs_browser(...) or (settings.media_transcript_panel_enabled and not req.transcribe and needs_transcript_panel(envelope))`, under the same `mode == "auto"` and `BROWSER_ENABLED` conditions as today. `needs_browser` itself is unchanged.
- **P13-S2, the render.** In the browser worker an `auto` item whose draft satisfies `needs_transcript_panel` is rendered by `Pipeline._panel_rendered(run, draft)` instead of `_render_and_extract`: the canonical watch URL (`draft.data.final_url`) with `RenderOptions(transcript_panel=True)` and otherwise the standard options (never unblock, no caller actions; the item has no browser options, or it would not have taken the media path). Compliance check of the canonical URL, the host's turn, `_render` and `_check_answers` as for any render.
- **P13-S3, the panel steps** (`browser_panel.py`, run by `_Render.run` after the load, the consent handling and the idle wait, before the HTML is read):
  1. Consent: when `ytd-consent-bump-v2-lightbox tp-yt-paper-dialog` is in the page, click its first button whose text is `Alle ablehnen` or `Reject all`; wait at most 5 s until the dialog's `opened` attribute is gone or a `SOCS` cookie is set. Visibility of the outer lightbox is not a test (its box is 0×0).
  2. Detect: when the page's HTML does not contain `PAmodern_transcript_view`, stop with `transcript_panel_unavailable`; nothing is clicked.
  3. Open: click `tp-yt-paper-button#expand` when it is visible, then the first visible `ytd-video-description-transcript-section-renderer button`; none visible within 5 s ends with `transcript_panel_unavailable`.
  4. Read: wait up to 15 s (bounded by the render's deadline) for a response whose URL path is `/youtubei/v1/get_panel`, status 200; read its body; a body over `MEDIA_SUBTITLE_MAX_BYTES`, another status or no response ends with `transcript_panel_unavailable`.
  The body goes to `Rendered.transcript_panel` (bytes). A failure is a warning, never an error of the render.
- **P13-S4, parsing** (`media/panel.py`, `parse_panel(body: bytes, *, max_bytes: int) -> list[Cue]`): the segments are `content.engagementPanelSectionListRenderer.content.sectionListRenderer.contents[*].itemSectionRenderer.contents[*].macroMarkersPanelItemViewModel.item.timelineItemViewModel.contentItems[0].transcriptSegmentViewModel`, text `simpleText`. Times come from the parallel list `frameworkUpdates.entityBatchUpdate.mutations[*].payload.timedMarkersListSyncEntity.timedListData.sections[0].timedSyncDataList[i]` (`videoTimeMs`, `endTimeMs`, strings of milliseconds) by the same index when both lists have the same length; otherwise from the segment's `timestamp` (`m:ss` or `h:mm:ss`) as start, the next segment's start as end (the last one ends at its start). Segments without text are skipped. Invalid JSON, a body over `max_bytes` or no segment path raise `PanelError`; an empty list is a valid "no text" answer.
- **P13-S5, the answer** (`pipeline_media.panel_result`). With cues that have text: the draft's result with `content` = `build_transcript(cues, draft.title, [], fmt)`, `quality` = `assess(None, content)`, `word_count`, `language` = `detect_language(None, None, content)`, `content_type` `text/vtt`, `mode_used` `browser`, `unblocked` false, `media.transcript_source` `transcript_panel` (D1 a), `media.transcript_language` = the detected language, `warnings` = the draft's without `subtitles_blocked` and `subtitles_failed`, `fingerprint` from the SHA-256 of the cues' text joined by newlines, judged by the envelope rules (a success is cached and fingerprinted like any transcript). Without cues, or with `transcript_panel_unavailable`: the draft unchanged but for the added warning `transcript_panel_unavailable`, never stored (as a refused draft is today). The rendered page's text is never converted or compared (`better` is not used here).
- **P13-S6, attempts and timings.** The render's document answers count in `attempts` on top of the draft's (as `_rendered` does); `render_ms` counts the panel steps.
- **P13-S7, metrics.** `text_extraction_media_transcript_panel_total{outcome}` with `outcome` in `transcript`, `unavailable`, `empty`: one count per panel render.
- **P13-S8, docs.** `docs/architecture.md` "Audio and video": D1 gets the package 13 addition (option c on the new view only, Jan 2026-10-31) and a ruling M7 "transcript panel" with P13-S1 to S7; "Browser worker" names the `transcript_panel` option. User guide `usage.md` (en, de): the YouTube paragraph says that a video whose subtitles the service could not read gets a second try in the browser where YouTube offers its new transcript view (`transcript_source: transcript_panel`, `mode_used: browser`), otherwise the details with the warning `transcript_panel_unavailable`. `parameters.md` (en, de) and the OpenAPI field descriptions list the new value and warning where they list the others. `docs/configuration.md` and `docs/handover.md` (environment reference) list `MEDIA_TRANSCRIPT_PANEL_ENABLED`. UI catalogues get `warning.transcript_panel_unavailable` (en, de) and any `transcript_source` label the UI shows. `CHANGELOG.md` ([0.1.0]) adds package 13; `docs/packages.md` gets row 13 and its description; README.md/README.de.md Status name packages 0-13. The release text (CHANGELOG, handover, packages.md) says `v0.1.0` is tagged on the merge of package 13.

## Review Focus

- A watch page without the new view (most videos today): nothing is clicked, the answer is the draft with `transcript_panel_unavailable`, within a few seconds, and nothing junk-like from the page's own text ever becomes `content`.
- A consent dialog that does not close, or a bot wall ("confirm you're not a bot") instead of the video: no endless wait; the draft with the warning.
- A `get_panel` answer that is not 200, not JSON, larger than `MEDIA_SUBTITLE_MAX_BYTES`, or whose two lists differ in length: the warning, or the `timestamp` fallback; never an exception out of the render.
- A cached draft, `transcribe: true`, `mode: fast`, `BROWSER_ENABLED=false` or `MEDIA_TRANSCRIPT_PANEL_ENABLED=false`: no escalation and no render.
- A video YouTube says has no subtitles (no `subtitles_*` warning) or a non-YouTube platform: no escalation.

---

### Task 1: The panel parser, the new source value and the setting

**Files:**
- Create: `src/text_extraction/media/panel.py`
- Modify: `src/text_extraction/media/types.py` (`TranscriptSource` gets `"transcript_panel"`)
- Modify: `src/text_extraction/settings.py` (`media_transcript_panel_enabled: bool = True`)
- Modify: `docs/configuration.md`, `deploy/helm/text-extraction/values.yaml` and the template that sets media env vars, `deploy/helm/test.sh` if it checks the media env vars, `docs/handover.md` environment reference
- Create: `tests/media/panel/get_panel.json` (a synthetic answer built from the spike's skeleton: 4 segments, the parallel time list, other branches elided; texts invented, no real transcript)
- Test: `tests/test_media_panel.py`, `tests/test_settings.py` (or the file that pins settings defaults), `tests/test_configuration_doc.py` stays green

**Interfaces:**
- Produces: `class PanelError(Exception)`; `def parse_panel(body: bytes, *, max_bytes: int) -> list[Cue]` (`Cue` from `media/transcript.py` or wherever `parse_captions` gets it); `TranscriptSource` includes `"transcript_panel"`; `Settings.media_transcript_panel_enabled: bool` (env `MEDIA_TRANSCRIPT_PANEL_ENABLED`, default `True`).

- [ ] **Step 1: Write the failing tests** in `tests/test_media_panel.py`:
  - `test_segments_with_times_from_the_sync_list`: the fixture gives 4 cues; the first is `Cue(start=18.0, end=23.98, text=<fixture text 1>)`.
  - `test_times_fall_back_to_the_timestamp_when_lists_differ`: drop one entry of `timedSyncDataList`; starts come from `timestamp` (`"0:18"` → 18.0, `"1:02:03"` → 3723.0), each end is the next start, the last end equals its start.
  - `test_segments_without_text_are_skipped`.
  - `test_no_segment_path_is_an_error`, `test_invalid_json_is_an_error`, `test_body_over_the_cap_is_an_error` (`max_bytes=10`): `PanelError`.
  - `test_an_empty_segment_list_is_no_cue`: `[]`.
  - In the settings test: `Settings().media_transcript_panel_enabled is True`, and `MEDIA_TRANSCRIPT_PANEL_ENABLED=false` turns it off.
- [ ] **Step 2: Run them, expect FAIL** (`uv run pytest tests/test_media_panel.py -q`).
- [ ] **Step 3: Implement** `parse_panel` per P13-S4 with `json.loads` and defensive `dict.get` walks (no exception but `PanelError` escapes); add the source value and the setting; document the setting (configuration.md: "Use YouTube's new transcript view in the browser when the subtitles could not be read (`auto` requests, needs `BROWSER_ENABLED`). Default `true`.") and add it to Helm values and the env template.
- [ ] **Step 4: Run** the new tests, `tests/test_configuration_doc.py`, `bash deploy/helm/test.sh` (source-built helm on PATH) and `uv run mypy`. Expect PASS.
- [ ] **Step 5: Commit** `feat: parse YouTube's new transcript view`.

### Task 2: The panel steps in the browser

**Files:**
- Create: `src/text_extraction/browser_panel.py`
- Modify: `src/text_extraction/render.py` (`RenderOptions.transcript_panel: bool = False`; `Rendered.transcript_panel: bytes | None = field(default=None, repr=False)`)
- Modify: `src/text_extraction/browser_render.py` (`_Render.run` calls the panel steps when `options.transcript_panel`; the warning and body go into `Rendered`)
- Modify: `tests/fakes.py` (`rendered(...)` and `FakeRenderer` can return a `transcript_panel` body and warnings)
- Test: `tests/browser/test_transcript_panel.py` (`pytest -m browser`), with a local fake watch page on pytest-httpserver in `tests/browser/youtube_site.py`

**Interfaces:**
- Consumes: Task 1's `parse_panel` only in a test assertion (the browser keeps raw bytes).
- Produces: `TRANSCRIPT_PANEL_UNAVAILABLE: Final = "transcript_panel_unavailable"` in `browser_panel.py`; `async def read_transcript_panel(page: Page, *, deadline: float, max_bytes: int) -> bytes | None` (None after logging the reason; the caller adds the warning); `RenderOptions.transcript_panel`; `Rendered.transcript_panel`.

The fake watch page (served from `127.0.0.1`, private networks allowed as the other browser tests do) has: a `<script>var ytInitialData = {...}</script>` that contains `"PAmodern_transcript_view"` or not (two routes), a `ytd-consent-bump-v2-lightbox` with an inner `tp-yt-paper-dialog opened` holding buttons "Alle akzeptieren" and "Alle ablehnen" (rejecting removes `opened` and sets `document.cookie = "SOCS=x"`), a full-page backdrop that intercepts clicks while the dialog is open, a hidden transcript button inside `ytd-video-description-transcript-section-renderer` that `tp-yt-paper-button#expand` reveals, and page JavaScript that on the button's click `fetch`es `POST /youtubei/v1/get_panel` (served from Task 1's fixture; a third route answers 400).

- [ ] **Step 1: Write the failing browser tests:**
  - `test_the_new_view_gives_the_get_panel_body`: `transcript_panel=True` on the page with the marker: `Rendered.transcript_panel` equals the fixture's bytes, no `transcript_panel_unavailable` warning, the server saw the consent decline before the click (the cookie).
  - `test_without_the_new_view_nothing_is_clicked`: no marker: `transcript_panel is None`, warning present, the server never got `get_panel`.
  - `test_a_failed_get_panel_is_a_warning`: 400 route: None plus the warning; the render itself succeeds.
  - `test_a_body_over_the_cap_is_a_warning`: `MEDIA_SUBTITLE_MAX_BYTES` set below the fixture's size.
  - `test_without_the_option_nothing_changes`: `transcript_panel=False` on the marker page: no click, no `get_panel` request, no warning.
- [ ] **Step 2: Run them, expect FAIL** (`uv run pytest -m browser tests/browser/test_transcript_panel.py -q`).
- [ ] **Step 3: Implement** `read_transcript_panel` per P13-S3 with Playwright locators (`page.locator(...).filter(has_text=...)`, `page.expect_response(lambda r: urlsplit(r.url).path == "/youtubei/v1/get_panel")`), every wait bounded by `deadline` (loop time) and the step's own cap; wire it into `_Render.run` and `Rendered`; extend the fakes.
- [ ] **Step 4: Run** the new browser tests, `uv run pytest -m browser tests/browser -q`, `uv run pytest tests/test_pipeline_render.py -q`, mypy, ruff. Expect PASS.
- [ ] **Step 5: Commit** `feat: read YouTube's new transcript view in the browser`.

### Task 3: Escalation and the answer

**Files:**
- Modify: `src/text_extraction/escalation.py` (`needs_transcript_panel`)
- Modify: `src/text_extraction/extract_api.py`, `src/text_extraction/worker.py`, `src/text_extraction/pipeline.py` (P13-S1 at each escalation decision; P13-S2 `_panel_rendered`; P13-S6)
- Modify: `src/text_extraction/pipeline_media.py` (`panel_result`, the metric of P13-S7)
- Modify: `src/text_extraction/extract_models.py` (field descriptions of `transcript_source` and `warnings` name the new value and warning)
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (`warning.transcript_panel_unavailable`; a label for `transcript_panel` if the UI labels sources)
- Test: `tests/test_escalation.py`, `tests/test_pipeline_media.py` (or a new `tests/test_pipeline_panel.py`), `tests/test_extract_api.py`, `tests/test_worker.py` (the auto-escalation tests' file)

**Interfaces:**
- Consumes: Task 1 (`parse_panel`, `PanelError`, `"transcript_panel"`, `Settings.media_transcript_panel_enabled`), Task 2 (`RenderOptions.transcript_panel`, `Rendered.transcript_panel`, `TRANSCRIPT_PANEL_UNAVAILABLE`).
- Produces: `def needs_transcript_panel(envelope: Envelope[ExtractResult]) -> bool` in `escalation.py`; `def panel_result(draft: Envelope[ExtractResult], cues: Sequence[Cue] | None, fmt: OutputFormat, *, warnings: Sequence[str]) -> Envelope[ExtractResult]` in `pipeline_media.py` (pure; `cues=None` or no text means the draft with the warning).

- [ ] **Step 1: Write the failing tests:**
  - escalation: true for a YouTube `metadata_only` fast result with `subtitles_blocked`, and with `subtitles_failed`; false without either warning, for `vimeo`, for `mode_used` `browser`, for `cache` `hit`, for a transcript result.
  - pipeline with `FakeRenderer`: an `auto` item with such a draft renders the canonical URL with `transcript_panel=True` and `unblock=False`; a fixture body gives `status: success`, `media.transcript_source == "transcript_panel"`, `mode_used == "browser"`, the draft's title as the transcript's heading, no `subtitles_*` warning, a fingerprint, and the result stored; a render without a body gives the draft with `transcript_panel_unavailable`, not stored; the rendered HTML (fake page text) never appears in `content`; `attempts` adds the render's answers.
  - extract_api and worker: an `auto` request whose fast media answer has `subtitles_blocked` is handed to the browser pool; not with `transcribe: true`, not with `MEDIA_TRANSCRIPT_PANEL_ENABLED=false`, not with `BROWSER_ENABLED=false`, not for `mode: fast`.
  - UI i18n tests stay green with the new keys (`uv run pytest tests/ui -q`).
- [ ] **Step 2: Run them, expect FAIL.**
- [ ] **Step 3: Implement** P13-S1, S2, S5, S6 and S7.
- [ ] **Step 4: Run** the full suite (`uv run pytest -q`), ruff, ruff format check, mypy. Expect PASS (known timing flakes may need a rerun alone).
- [ ] **Step 5: Commit** `feat: try YouTube's transcript view in the browser when the subtitles could not be read`.

### Task 4: Docs, release text and the done-when evidence

**Files:**
- Modify: `docs/architecture.md`, `docs/user-guide/en/usage.md`, `docs/user-guide/de/usage.md`, `docs/user-guide/en/parameters.md`, `docs/user-guide/de/parameters.md`, `docs/configuration.md` (if Task 1 left wording), `docs/handover.md`, `docs/packages.md`, `CHANGELOG.md`, `README.md`, `README.de.md`
- Test: `tests/test_docs_i18n.py`, `tests/test_handover_doc.py`, `tests/test_configuration_doc.py`, `uv run python scripts/check_translation_pairs.py --base origin/main`

- [ ] **Step 1:** Write P13-S8 into the docs (English first, then the German partner of each English change).
- [ ] **Step 2:** `docs/packages.md`: row 13 "YouTube transcript panel", status "in review", PR, done-when below; the package table's description and done-when; a "Package 13 note" with Jan's decisions and the spike link.
- [ ] **Step 3:** Run the doc tests and the pair check. Expect PASS.
- [ ] **Step 4: Commit** `docs: describe the YouTube transcript panel`.

**Done when:** the unit, pipeline and browser tests above pass in CI (`ci`, `browser`); the live workflow's media set runs and is reported (not gated: the GitHub runners' addresses may meet a bot wall, which must end as the draft with the warning); and on Jan's PC (`docker compose up` with the browser worker) the video `XAgqWLNlseg` answers `transcript_source: transcript_panel` with its segments while the media children cannot read its subtitles, and a video without the new view answers the draft with `transcript_panel_unavailable`. From Jan's line the children normally read the subtitles, so for the check the controller makes the probe fail with a setting (`MEDIA_EXTRACT_MAX_BYTES=1024`, the lowest allowed value, which ends the probe with `bytes_cap` and the warning `subtitles_failed`; the value is confirmed against the settings' bounds before the check).
