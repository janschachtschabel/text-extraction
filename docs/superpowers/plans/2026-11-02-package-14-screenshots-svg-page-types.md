# Package 14: Screenshot Formats, SVG Previews and Page Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screenshots in other formats and sizes (preview images), SVG images in the playground's
preview, a page type for every HTML page and a category for every link (site structure and
`/v1/extract`), and a measured decision on one more browser-escalation trigger.

**Architecture:** Screenshots stay a lossless PNG capture in the browser; the browser worker's
pipeline then scales and encodes it with Pillow before storing the artifact. A new viewport per
request is set on the page before navigation. SVG previews are recognised by a text sniff and shown
as `data:image/svg+xml` inside `<img>` (no script runs there). Page types come from one pure
rule-based module (`page_types.py`) that both the extract pipeline and the site crawl call.

**Tech Stack:** Python 3.13, FastAPI, pydantic, Playwright/Patchright, Pillow 13, lxml,
BeautifulSoup, Jinja2/htmx, pytest.

**Spec:** Jan's approved plan (German), `/mnt/project-files/package-14/plan-paket-14.md`
(approved 2026-11-02 with D1-D6 = a). Its decisions are copied into Global Constraints.

## Global Constraints

- CLAUDE.md rules hold: English code and docs; user docs and UI texts in English **and** German in
  the same PR (`docs/user-guide/en|de/`, `ui/locales/en.json`, `de.json`); result envelope
  unchanged; SSRF guard on every fetch; no GPL/AGPL dependency; no new state outside Valkey.
- D1: this package is functions only. The visual redesign is package 15 (separate plan, PR).
- D2: screenshot `format` `png|jpeg|webp` (default `png`), `quality` 1-100 (default 80, only
  `jpeg`/`webp`), `viewport` `{width: 320-3840, height: 240-2160}` (default 1920x1080), output
  `width` 16-3840 (downscale only, aspect ratio kept; default none). Pillow does scaling and
  encoding. Playground presets: whole page; visible area 1920x1080; social preview 1200x630;
  small preview 400 wide; mobile 390x844; own values.
- D3: SVG only in the playground preview (`<img src="data:image/svg+xml;base64,...">`); image
  descriptions keep skipping SVG (they report counts only, so no new reason is needed: ruling
  P14-S3).
- D4: rule-based, no LLM. Page types: `home`, `content`, `listing`, `imprint`, `privacy`, `legal`,
  `contact`, `about`, `auth`, `search`, `error`, `other`. Link categories: the same minus `error`
  and `other`, plus `social`, `download`, `nav`. Both in site structure and `/v1/extract`. The
  site tree shows a badge per URL and can select "content pages" (all but `imprint`, `privacy`,
  `legal`, `contact`, `auth`, `search`, `error`).
- D5: escalate a 2xx HTML fast result that is `content_too_short` or `empty_content` and has at
  least one executable script, **only if** an A/B live run shows `wlo_200` auto not lower than
  main (and the median time does not rise by more than 20 %). Otherwise revert the rule and
  record the numbers in the PR.
- D6 (package 15): system fonts. Not in this package.
- Every new response field has a default that keeps results cached before this package valid
  (`None`), like `screenshot`, `document` and `media` today.
- A full-page capture never holds more pixels than today's maximum, 1920 x
  `SCREENSHOT_MAX_HEIGHT_PX` (ruling P14-S1): its clip height is
  `min(SCREENSHOT_MAX_HEIGHT_PX, SCREENSHOT_MAX_HEIGHT_PX * 1920 // viewport_width)`.

## Review Focus

1. A huge full-page capture at a wide viewport (3840 wide, 16384 tall page): pixel cap P14-S1 holds,
   Pillow never decodes more than 1920 x `SCREENSHOT_MAX_HEIGHT_PX` pixels, `screenshot_clipped`
   is set. Test in Task 2.
2. `width` larger than the captured width (upscale) and `quality` with `png`: 422 with a clear
   message, never a silent ignore. Test in Task 1.
3. An SVG that carries `<script>` or an external `<image href="https://...">`: shown only inside
   `<img>`, never inlined, never as a link target; the CSP stays `img-src 'self' data:`. Test in
   Task 5.
4. A page whose URL says one type and whose content says another (`/kontakt` that is a long
   article; `/` that is a 404): the documented precedence decides, and `signals` say why. Tests in
   Task 6.
5. Results cached before this package (no `page_type`, links without `category`) and jobs stored
   before it (options without `screenshot_options`) still load and render in the UI. Tests in
   Tasks 1, 7 and 8.

---

### Task 1: Screenshot options in the API models

**Files:**
- Modify: `src/text_extraction/browser_options.py` (new `Viewport`, `ScreenshotOptions`)
- Modify: `src/text_extraction/extract_models.py` (`ExtractRequest.screenshot_options`,
  `_URL_ONLY_OPTIONS`)
- Modify: `src/text_extraction/job_models.py` (`JobOptions.screenshot_options`)
- Modify: `src/text_extraction/browser_models.py` (`ScreenshotRequest.viewport`, `format`,
  `quality`, `width`)
- Modify: `src/text_extraction/screenshot_api.py` (`run_screenshot` maps the fields)
- Modify: `src/text_extraction/api_examples.py` (a `preview` example: 1200x630, webp, width 600)
- Test: `tests/test_browser_models.py`, `tests/test_extract_models.py`, `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `class Viewport(BaseModel, extra="forbid", strict=True)`: `width: int` (320-3840),
    `height: int` (240-2160).
  - `DEFAULT_VIEWPORT: Final = Viewport(width=1920, height=1080)`
  - `ScreenshotFormat = Literal["png", "jpeg", "webp"]`
  - `class ScreenshotOptions(BaseModel, extra="forbid", strict=True)`: `viewport: Viewport =
    DEFAULT_VIEWPORT`, `format: ScreenshotFormat = "png"`, `quality: int | None = None` (1-100),
    `width: int | None = None` (16-3840). Validators: `quality` only with `jpeg`/`webp`
    (message `"quality needs format jpeg or webp"`); `width` at most `viewport.width` (message
    `"width cannot be larger than the viewport's width"`). Method
    `effective_quality() -> int`: `quality or 80`.
  - `ExtractRequest.screenshot_options: ScreenshotOptions | None = None`; a validator refuses it
    without `screenshot: true` (`"screenshot_options needs screenshot"`), so it is also refused in
    `mode: fast`.
  - `JobOptions.screenshot_options: ScreenshotOptions | None` with
    `exclude_if=lambda value: value is None` (as `site`), so stored jobs and the workers'
    `ExtractRequest(**options.model_dump())` are unchanged when it is unset.
  - `ScreenshotRequest`: `viewport: Viewport | None = None`, `format: ScreenshotFormat = "png"`,
    `quality: int | None = None`, `width: int | None = None`; `run_screenshot` builds
    `ScreenshotOptions` from them, and sends `screenshot_options=None` when all four are at their
    defaults (keeps today's request identical).

- [ ] **Step 1: Write the failing tests**

```python
def test_screenshot_request_builds_options():
    body = ScreenshotRequest(
        url="https://example.org/",
        viewport={"width": 1200, "height": 630},
        format="webp",
        quality=70,
        width=600,
    )
    opts = screenshot_options_of(body)  # helper in screenshot_api, used by run_screenshot
    assert opts == ScreenshotOptions(
        viewport=Viewport(width=1200, height=630), format="webp", quality=70, width=600
    )


def test_screenshot_request_defaults_send_no_options():
    assert screenshot_options_of(ScreenshotRequest(url="https://example.org/")) is None


@pytest.mark.parametrize(
    "body, message",
    [
        ({"format": "png", "quality": 50}, "quality needs format jpeg or webp"),
        ({"viewport": {"width": 800, "height": 600}, "width": 801}, "larger than the viewport"),
        ({"viewport": {"width": 319, "height": 600}}, "greater than or equal to 320"),
        ({"width": 15}, "greater than or equal to 16"),
    ],
)
def test_screenshot_options_refused(body, message): ...  # ValidationError containing message


def test_extract_request_refuses_options_without_screenshot(): ...
def test_job_options_dump_has_no_screenshot_options_when_unset():
    assert "screenshot_options" not in JobOptions(compliance="enforce").model_dump()


def test_job_options_round_trip_with_screenshot_options(): ...  # JobStore stores and reads it
```

Also: `POST /v1/screenshot` with `quality` and `format: png` answers 422 (in
`tests/test_extract_api.py` or the screenshot API test module where the route is tested today).

- [ ] **Step 2: Run them, expect FAIL** (`uv run pytest tests/test_browser_models.py tests/test_extract_models.py tests/test_jobs.py -q`)
- [ ] **Step 3: Implement** the models, `screenshot_options_of(body: ScreenshotRequest) -> ScreenshotOptions | None` in `screenshot_api.py`, and the OpenAPI descriptions (state the pixel cap and the downscale-only rule in `width`'s description; update the docstring of `POST /v1/screenshot`, which says "1920 px wide").
- [ ] **Step 4: Run them, expect PASS**, plus `uv run pytest tests/test_openapi*.py -q` if the OpenAPI is snapshot-tested.
- [ ] **Step 5: Commit** `feat(screenshot): viewport, format, quality and width options`

### Task 2: Encode and store the screenshot in its format

**Files:**
- Create: `src/text_extraction/screenshot_image.py`
- Modify: `src/text_extraction/render.py` (`ArtifactSink.put_screenshot` signature)
- Modify: `src/text_extraction/browser_worker.py` (`JobArtifacts.put_screenshot`)
- Modify: `src/text_extraction/jobs.py` (`SCREENSHOT_ARTIFACTS`, `ARTIFACT_NAMES`)
- Modify: `src/text_extraction/pipeline.py` (`_store_screenshot`)
- Modify: `src/text_extraction/screenshot_api.py` (`_MEDIA_TYPES`, the 200 content types)
- Modify: `pyproject.toml` (`pillow==13.0.0` as a direct runtime dependency: the browser worker
  imports it; today it only comes in through markitdown's extras), `uv.lock`
- Test: `tests/test_screenshot_image.py`, `tests/test_pipeline_render.py`,
  `tests/test_browser_worker.py`, `tests/test_jobs.py`

**Interfaces:**
- Consumes: `ScreenshotOptions` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class EncodedScreenshot`: `data: bytes`, `width: int`,
    `height: int`, `content_type: str`, `name: str` (`screenshot.png|jpg|webp`); `__repr__`
    without the bytes.
  - `def encode_screenshot(png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot`:
    `None` or `png` without `width` passes the bytes through untouched (size read with
    `png_size`); otherwise Pillow opens the PNG (`Image.MAX_IMAGE_PIXELS` stays at its default),
    scales to `width` with `Image.Resampling.LANCZOS` keeping the ratio (height rounded, at least
    1), and saves as PNG (`optimize=False`), JPEG (`quality`, RGB, `progressive=True`) or WebP
    (`quality`, `method=4`).
  - `ArtifactSink.put_screenshot(self, image: EncodedScreenshot) -> ArtifactRef` (was
    `png, width, height`); `JobArtifacts` stores under `image.name` with `image.content_type`.
  - `jobs.SCREENSHOT_ARTIFACTS: Final = ("screenshot.png", "screenshot.jpg", "screenshot.webp")`;
    `ARTIFACT_NAMES = SCREENSHOT_ARTIFACTS` (expiry and deletion cover all three);
    `SCREENSHOT_ARTIFACT` stays `"screenshot.png"` for existing callers.
  - `_store_screenshot` encodes with `await asyncio.to_thread(encode_screenshot, png,
    req.screenshot_options)` before the `SCREENSHOT_MAX_BYTES` check (the limit applies to the
    stored file).

- [ ] **Step 1: Write the failing tests** (`tests/test_screenshot_image.py`; build PNGs with Pillow in the test)

```python
def test_png_without_width_is_passed_through():
    png = _png(800, 600)
    out = encode_screenshot(png, None)
    assert out.data is png and (out.width, out.height) == (800, 600)
    assert (out.content_type, out.name) == ("image/png", "screenshot.png")


@pytest.mark.parametrize(
    "fmt, mime, name, magic",
    [
        ("jpeg", "image/jpeg", "screenshot.jpg", b"\xff\xd8\xff"),
        ("webp", "image/webp", "screenshot.webp", b"RIFF"),
    ],
)
def test_formats(fmt, mime, name, magic): ...  # data starts with magic; Pillow reopens it


def test_width_scales_down_keeping_ratio():
    out = encode_screenshot(
        _png(1200, 630),
        ScreenshotOptions(viewport=Viewport(width=1200, height=630), format="webp", width=600),
    )
    assert (out.width, out.height) == (600, 315)


def test_quality_changes_size(): ...  # quality 20 file smaller than quality 95
```

Pipeline test (`tests/test_pipeline_render.py`, fake renderer and fake sink as the existing
screenshot tests): a request with `screenshot_options` format `jpeg` stores `screenshot.jpg` with
`content_type` `image/jpeg`; an encoded file over `SCREENSHOT_MAX_BYTES` fails as today.
Job store test: `put_artifact` and `artifact` with `screenshot.webp`; expiry (`shorten`) covers it.
Artifact route test: `GET .../artifacts/screenshot.webp` answers `image/webp`.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** as in Interfaces. Run `uv lock` after adding Pillow.
- [ ] **Step 4: Run** `uv run pytest tests/test_screenshot_image.py tests/test_pipeline_render.py tests/test_browser_worker.py tests/test_jobs.py -q`, expect PASS; `uv run python scripts/check_licenses.py` stays green.
- [ ] **Step 5: Commit** `feat(screenshot): encode as png, jpeg or webp and scale with Pillow`

### Task 3: Viewport in the browser

**Files:**
- Modify: `src/text_extraction/render.py` (`RenderOptions.viewport: tuple[int, int] = (1920, 1080)`)
- Modify: `src/text_extraction/pipeline_render.py` (pass the request's viewport)
- Modify: `src/text_extraction/browser_render.py` (`_screenshot`, the page set-up)
- Test: `tests/browser/test_screenshot_viewport.py` (`pytest -m browser`)

**Interfaces:**
- Consumes: `ScreenshotOptions.viewport` (Task 1), `encode_screenshot` (Task 2).
- Produces: `RenderOptions.viewport`. When it differs from `VIEWPORT`, the render calls
  `page.set_viewport_size({"width": w, "height": h})` before the navigation, in both the standard
  and the unblock browser (unblock contexts have `no_viewport`; the window stays 1920x1080).
  `_screenshot` clips a full page at the viewport's width and at
  `min(limit, limit * 1920 // width)` rows (P14-S1); the `screenshot_clipped` warning is set when
  the page's height is over that.

- [ ] **Step 1: Write the failing browser tests** against a local page on pytest-httpserver (as in `tests/browser/`):
  - viewport 390x844, `full_page: false`: the PNG is 390x844; a page with a CSS media query
    `max-width: 600px` that hides `#desktop` shows the mobile variant (check the rendered HTML).
  - viewport 1200x630, `format: webp`, `width: 600`: the stored artifact is a WebP of 600x315.
  - viewport 3840x1080 on a 20,000 px tall page: the capture is 3840 x 8192 (with
    `SCREENSHOT_MAX_HEIGHT_PX=16384`) and carries `screenshot_clipped`.
  - unblock (Patchright, as `tests/browser/test_maximum_extraction.py`): viewport 390x844 gives a
    390 px wide capture.
- [ ] **Step 2: Run** `uv run pytest -m browser tests/browser/test_screenshot_viewport.py -q`, expect FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**; then the whole browser suite `uv run pytest -m browser tests/browser -q`
- [ ] **Step 5: Commit** `feat(browser): per-request viewport for screenshots`

### Task 4: Screenshot options in the playground and MCP

**Files:**
- Modify: `src/text_extraction/ui/templates/forms/screenshot.html`, `src/text_extraction/ui/forms.py`
  (`_screenshot_fields`), `src/text_extraction/ui/answers.py` (`read_screenshot`: PNG, JPEG or
  WebP by `sniff_image`; message key `screenshot.not_image` replaces `screenshot.not_png`)
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json`
- Modify: `src/text_extraction/mcp_server/results.py`, `tools_extract.py`, `tools_jobs.py` (the
  image content's MIME type from `ArtifactRef.content_type`; `get_job_screenshot` reads the name
  from the job's result instead of assuming `screenshot.png`)
- Test: `tests/ui/test_forms.py` (or the module that tests `_screenshot_fields` today),
  `tests/ui/test_done_when.py`, `tests/mcp_server/`

**Interfaces:**
- Consumes: the `ScreenshotRequest` fields (Task 1).
- Produces: form field `preset` with values `page` (default: full page, png), `viewport`
  (1920x1080, `full_page` false), `social` (1200x630, `full_page` false, webp, quality 80),
  `thumb` (1280x800, `full_page` false, webp, `width` 400), `mobile` (390x844, `full_page` true,
  png), `custom` (fields `viewport_width`, `viewport_height`, `format`, `quality`, `width`,
  `full_page` shown in a fieldset that is only enabled with `custom`, via `ui.js`; without JS all
  fields stay usable and `custom` reads them). `_screenshot_fields` maps a preset to the REST body
  and leaves out every field at its default.

- [ ] **Step 1: Failing tests:** each preset gives the exact REST body (table test); `custom` with `quality` and `png` shows the 422's message at the `quality` control; the result view shows a JPEG and a WebP screenshot as `data:` image; MCP `screenshot_url` with `{"format": "webp", "width": 400}` attaches an `image/webp` image.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**, with the texts in both catalogues (`tests/ui/test_i18n.py`, `tests/ui/test_templates_i18n.py`).
- [ ] **Step 4: Run** `uv run pytest tests/ui tests/mcp_server -q` and `uv run pytest -m browser tests/ui -q` (axe and keyboard), expect PASS
- [ ] **Step 5: Commit** `feat(ui): screenshot presets and formats`

### Task 5: SVG images in the preview

**Files:**
- Modify: `src/text_extraction/vision/images.py` (`is_svg`)
- Modify: `src/text_extraction/vision/fetch.py` (`ImageFetcher.get(..., allow_svg: bool = False)`,
  `ImageBytes.media_type` widened to `VisionMediaType | Literal["image/svg+xml"]`)
- Modify: `src/text_extraction/ui/previews.py` (`allow_svg=True`)
- Test: `tests/vision/test_images.py` (or where `sniff_image` is tested), `tests/vision/test_fetch.py`,
  `tests/ui/test_previews.py`

**Interfaces:**
- Produces: `def is_svg(body: bytes) -> bool`: decode the first 4,096 bytes as UTF-8 (BOM
  allowed, errors ignored), skip whitespace, an XML declaration, comments and a `<!DOCTYPE svg`,
  then the root must start with `<svg` followed by whitespace, `>` or `/` (also with a namespace
  prefix `<x:svg`). `ImageFetcher.get` returns `ImageBytes(media_type="image/svg+xml")` for such
  a body only when `allow_svg`; the vision step calls without it, so nothing it does changes.

- [ ] **Step 1: Failing tests:** `is_svg` true for plain, with XML declaration, with BOM, with doctype and comment, with prefix; false for HTML containing an inline `<svg>`, for `<svgfoo>`, for a PNG, for empty bytes. `ImageFetcher.get` on an SVG body: `ImageSkipped("unsupported")` without `allow_svg`, `ImageBytes` with it. `load_previews` on an SVG with `<script>alert(1)</script>` and an external `<image href>` gives a `data:image/svg+xml;base64,` URI, and the rendered images fragment contains it only as an `<img src>`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/vision tests/ui -q`, expect PASS; the CSP header test still pins `img-src 'self' data:`.
- [ ] **Step 5: Commit** `feat(ui): show SVG images in the preview`

### Task 6: The page type and link category rules

**Files:**
- Create: `src/text_extraction/page_types.py` (pure: no I/O)
- Test: `tests/test_page_types.py`

**Interfaces:**
- Produces:
  - `PageType = Literal["home", "content", "listing", "imprint", "privacy", "legal", "contact",
    "about", "auth", "search", "error", "other"]`
  - `LinkCategory = Literal["home", "content", "listing", "imprint", "privacy", "legal",
    "contact", "about", "auth", "search", "social", "download", "nav"]`
  - `PageSignal = Literal["status", "soft_404", "root_path", "schema_org", "og_type", "url",
    "title", "h1", "password_field", "pagination", "link_density", "word_count"]`
  - `class PageTypeInfo(BaseModel)`: `type: PageType`, `signals: list[PageSignal]` (the signals
    that agree with the chosen type, in the order below).
  - `def url_type(url: str) -> PageType | None`: the type the URL's path and query say
    (`imprint`, `privacy`, `legal`, `contact`, `about`, `auth`, `search`, `listing`, `home` for an
    empty or `/` path or `/index.(html|htm|php)`), else None.
  - `def link_category(url: str, text: str, kind: Literal["internal", "external", "file"]) ->
    LinkCategory`
  - `def page_type(html: str, url: str, *, status: int | None, quality: Quality,
    min_words: int) -> PageTypeInfo`
- URL patterns (case-insensitive, matched on a whole path segment or a segment's stem before
  `.html`/`.php`; German umlauts and their `ae/oe/ue` spellings):
  - `imprint`: `impressum`, `imprint`, `legal-notice`, `site-notice`, `anbieterkennzeichnung`
  - `privacy`: `datenschutz`, `datenschutzerklaerung`, `datenschutzerklärung`,
    `datenschutzhinweise`, `privacy`, `privacy-policy`, `privacy-notice`, `dsgvo`, `gdpr`
  - `legal`: `agb`, `terms`, `terms-of-service`, `terms-of-use`, `nutzungsbedingungen`, `cookies`,
    `cookie-policy`, `cookie-richtlinie`, `disclaimer`, `haftungsausschluss`, `rechtliches`,
    `legal`, `widerruf`, `widerrufsrecht`, `barrierefreiheit`, `barrierefreiheitserklaerung`,
    `erklaerung-zur-barrierefreiheit`, `accessibility`, `accessibility-statement`
  - `contact`: `kontakt`, `contact`, `contact-us`, `kontaktformular`
  - `about`: `ueber-uns`, `uber-uns`, `über-uns`, `about`, `about-us`, `wir-ueber-uns`, `team`,
    `wer-wir-sind`, `who-we-are`
  - `auth`: `login`, `logout`, `signin`, `sign-in`, `signout`, `sign-out`, `signup`, `sign-up`,
    `register`, `registrieren`, `registrierung`, `anmelden`, `anmeldung`, `abmelden`, `account`,
    `my-account`, `mein-konto`, `konto`, `profil`, `profile`, `passwort`, `password`,
    `password-reset`
  - `search`: path segment `search`, `suche`, `recherche`, or a query parameter `q`, `query`,
    `search`, `suche`, `s`, `keyword` with a value
  - `listing`: segment `kategorie`, `category`, `categories`, `tag`, `tags`, `schlagwort`,
    `archiv`, `archive`, `page/<n>`, `seite/<n>`, or a query `page=<n>`/`seite=<n>`
  - Order when several match: `imprint`, `privacy`, `legal`, `auth`, `search`, `contact`,
    `about`, `listing`.
- `link_category`: `download` for `kind == "file"`; `social` for an `external` link whose host is
  (or is below) one of: `facebook.com`, `instagram.com`, `x.com`, `twitter.com`, `linkedin.com`,
  `xing.com`, `youtube.com`, `youtu.be`, `vimeo.com`, `tiktok.com`, `pinterest.com`,
  `mastodon.social`, `bsky.app`, `threads.net`, `t.me`, `telegram.me`, `whatsapp.com`, `wa.me`,
  `reddit.com`, `github.com`, `flickr.com`, `soundcloud.com`, `twitch.tv`, `discord.com`,
  `discord.gg`; then `url_type`; then `nav` when the collapsed, case-folded text is one of the old
  service's nav texts (`home`, `start`, `startseite`, `nach oben`, `back to top`, `top`, `menu`,
  `menü`, `navigation`, `zurück`, `back`, `übersicht`, `overview`, `sitemap`, arrows `→ ← › ‹ » «
  ▲`); else `content`.
- `page_type` precedence (first that applies wins; ruling P14-S4):
  1. `error`: `status` >= 400, or `quality.soft_404` (signals `status`/`soft_404`).
  2. `home`: `url_type(url) == "home"` (signal `root_path`).
  3. schema.org (JSON-LD `@type` or microdata `itemtype` of the page's main entity, read with
     lxml from the HTML): `ContactPage` -> `contact`, `AboutPage` -> `about`,
     `SearchResultsPage` -> `search`, `CollectionPage` -> `listing` (signal `schema_org`).
  4. `url_type` for `imprint`, `privacy`, `legal`, `auth`, `search`, `contact`, `about`,
     `listing` (signal `url`).
  5. The `<title>` or the first `<h1>` whose normalised text equals or starts with a type's word
     (`Impressum`, `Imprint`, `Datenschutz`, `Datenschutzerklärung`, `Privacy Policy`, `AGB`,
     `Nutzungsbedingungen`, `Terms of Use`, `Kontakt`, `Contact`, `Über uns`, `About us`,
     `Anmelden`, `Login`, `Registrieren`, `Suche`, `Search results`, `Suchergebnisse`) followed by
     nothing or a separator (`|`, `-`, `–`, `:`, `·`) (signals `title`/`h1`).
  6. `auth`: a password input and fewer than `THIN_PAGE_BELOW_WORDS` words (signal
     `password_field`).
  7. `listing`: `<link rel="next|prev">` or `<a rel="next|prev">` (signal `pagination`), or the
     text inside `<a>` is at least 60 % of the visible text and the page has at least 20 links
     (signal `link_density`; constant `LISTING_LINK_SHARE = 0.6`, `LISTING_MIN_LINKS = 20`).
  8. `content` when `quality.word_count >= min_words` (signal `word_count`), else `other`.
  Signals that agree with the winner but come later are added too (so `/impressum` titled
  "Impressum" has `["url", "title"]`).

- [ ] **Step 1: Write the failing tests:** a parametrised table of at least 40 URLs (German and English, each type, segment vs substring: `/impressum-archiv-2019` is not `imprint` but `/impressum.html` is; `/blog/team-building-tipps` is not `about`; `?s=` without value is not `search`); `link_category` for social subdomains (`de-de.facebook.com`), an internal link to `/` (`home`), a PDF (`download`), "Startseite" (`home` by URL wins over `nav`); `page_type` for each precedence step with minimal HTML, including the conflicts of Review Focus 4 and a JSON-LD `ContactPage` at `/service`.
- [ ] **Step 2: Run** `uv run pytest tests/test_page_types.py -q`, expect FAIL
- [ ] **Step 3: Implement** with compiled patterns at import; parse once with `lxml.html` (reuse `structured.rules.parse_page` if it fits).
- [ ] **Step 4: Run, expect PASS**; `uv run mypy` clean
- [ ] **Step 5: Commit** `feat: rule-based page types and link categories`

### Task 7: Page types and link categories in the results

**Files:**
- Modify: `src/text_extraction/extract_models.py` (`ExtractResult.page_type: PageTypeInfo | None = None`)
- Modify: `src/text_extraction/convert/links.py` (`Link.category: LinkCategory | None = None`)
- Modify: `src/text_extraction/site/types.py` (`SiteLink.category: LinkCategory | None = None`),
  `src/text_extraction/site/links.py`
- Modify: `src/text_extraction/pipeline.py` (`_process` sets `page_type` for every HTML page:
  fast and rendered; not for documents, media, plain text, Markdown or uploads)
- Modify: `tests/site/sites/a/` (pages `impressum.html`, `datenschutz/`, `kontakt.html`,
  `login.html`, `kategorie/physik/` listing, a footer with a Mastodon and a YouTube link)
- Test: `tests/test_pipeline.py`, `tests/site/test_links.py` (or the module that tests
  `site_links`), `tests/site/test_done_when.py`, `tests/test_convert_links.py`

**Interfaces:**
- Consumes: `page_type`, `link_category` (Task 6).
- Produces: `data.page_type` on `/v1/extract`, `/v1/batch` items and `site` page items (the runner
  keeps it when it drops the content); `category` on every entry of `data.links` and
  `data.site.links`. The status passed to `page_type` is `upstream_status`.

- [ ] **Step 1: Failing tests:** a fast extract of an `/impressum` page answers `page_type.type == "imprint"`; a PDF answers `page_type: null`; `links: true` gives categories; a stored cache entry without the new fields loads (`ExtractResult.model_validate` of a dict without them); the done-when test of site `a` asserts the page types of the new pages and the `social` category of the footer links.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**; describe both fields in the OpenAPI (`page_type`: "HTML pages only; null otherwise"; the type list; `signals`).
- [ ] **Step 4: Run** `uv run pytest tests/test_pipeline.py tests/site tests/test_convert_links.py -q`, expect PASS
- [ ] **Step 5: Commit** `feat: page_type and link category in extract and site results`

### Task 8: Page types in the site tree

**Files:**
- Modify: `src/text_extraction/ui/site_tree.py` (`TreeNode.page_type`, `TreeNode.type_guessed`,
  `SiteTree.type_counts`)
- Modify: `src/text_extraction/ui/templates/tree.html`, `src/text_extraction/ui/static/ui.js`,
  `src/text_extraction/ui/static/ui.css`
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (`page_type.<value>` for every
  type and category, `tree.select_content`, `tree.types`, `tree.type_guessed`)
- Test: `tests/ui/test_site_tree.py`, `tests/ui/test_done_when.py`, browser tests in `tests/ui`

**Interfaces:**
- Consumes: `data.page_type` of page items; `url_type` (Task 6) for sitemap-only and linked-only
  URLs (`type_guessed = True`; a link's own `category` when it is one of the page types).
- Produces: each URL leaf has `data-page-type="<type>"` on its checkbox and a badge with the
  translated type (guessed ones with the hint "guessed from the URL"); a line "Types: Content 120,
  Listing 14, Imprint 1, ..." over the tree; a button "Select content pages" (`data-action=
  "tree-content"`) that ticks every leaf whose type is not in `imprint`, `privacy`, `legal`,
  `contact`, `auth`, `search`, `error`, and unticks those, then updates the counter as
  `tree-all` does. An old job's items without `page_type` show no badge.

- [ ] **Step 1: Failing tests:** `TreeBuilder` gives the types and counts for crawled, sitemap and linked leaves; an item without `page_type` has `page_type None`; the fragment has the badges, the counts and the button in `en` and `de`; a Chromium test clicks "Select content pages" and checks that the imprint stays unticked and the counter matches.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q` (axe 0 violations, keyboard), expect PASS
- [ ] **Step 5: Commit** `feat(ui): page types in the site tree`

### Task 9: One more escalation trigger, measured (D5)

**Files:**
- Modify: `src/text_extraction/escalation.py` (`needs_browser`)
- Modify: `tests/live/run_site.py` (report the page type distribution per site)
- Test: `tests/test_escalation.py`, `tests/test_live_site.py`

**Interfaces:**
- Produces: `needs_browser(envelope, unblock=False)` is also true when the result is 2xx HTML
  (`_is_ok_html`), `envelope.error.code` is `CONTENT_TOO_SHORT` or `EMPTY_CONTENT`, and
  `result.quality.script_count >= 1`. Everything else in the function stays.

- [ ] **Step 1: Failing tests:** escalates a 2xx HTML `content_too_short` with a script; not without a script; not for a non-HTML result; not for a 404; not for a cache hit.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**; `run_site.py` summary gains a "page types" table (type, pages) per site.
- [ ] **Step 4: Run** `uv run pytest tests/test_escalation.py tests/test_live_site.py -q`, expect PASS
- [ ] **Step 5: Commit** `feat(escalation): try short pages with scripts in the browser`
- [ ] **Step 6: A/B live run:** label the PR `live` (branch) and run `live.yml` with `service_ref: main` (the baseline). Compare `wlo_200` auto ok rate and median time. Keep the rule only if the branch's rate is not lower and the median time is at most 20 % higher; otherwise revert this task's `escalation.py` change (keep the tests that pin today's behaviour by inverting them) and record both runs in the PR.

### Task 10: Docs, changelog and package status

**Files:**
- Modify: `docs/user-guide/en|de/single-url.md` (screenshots: formats, sizes, presets; page
  type), `parameters.md` (the new fields), `whole-site-and-metadata.md` and `part-of-a-site.md`
  (page types, link categories, "Select content pages"), `glossary.md` (page type, link category)
- Modify: `docs/architecture.md` (screenshot encoding, P14-S1, page types and their precedence,
  the escalation rule if kept), `docs/packages.md` (rows 14 and 15; status row 14),
  `CHANGELOG.md` (`[Unreleased]`), `README.md`/`README.de.md` if they list screenshot features
- Test: `uv run pytest tests/test_docs_i18n.py tests/test_configuration_doc.py -q`,
  `uv run python scripts/check_translation_pairs.py`

- [ ] **Step 1: Write the docs** (English first, then the German partner in the same commit)
- [ ] **Step 2: Run the doc tests**, expect PASS
- [ ] **Step 3: Commit** `docs: package 14`

## Done when

- `uv run pytest`, `uv run pytest -m browser` (CI `ci`, `browser`), `ruff check`, `ruff format
  --check` and `mypy` green; the licence check green.
- Live run on the branch: `wlo_200` auto at or above 80 %; the site run reports page types for the
  five WLO sites; Task 9's A/B result is in the PR.
- Check on Jan's PC (`docker compose up --build`): a 1200x630 WebP preview and a 400 px thumbnail
  from the playground, an SVG in the image preview, page types and "Select content pages" in the
  structure of a WLO site.
