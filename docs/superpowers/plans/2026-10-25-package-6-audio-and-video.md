# Package 6: Audio and Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audio and video become text:
- `POST /v1/extract` and `POST /v1/batch` answer a YouTube, Vimeo or Mediathek URL with the video's subtitles (manual first, then automatic captions), its title, description, duration and licence; without subtitles with its metadata (`metadata_only`).
- An HTML page whose `<video>` or `<audio>` carries a `<track>` gets that track's text after its own; players embedded in a page are listed in `data.media.embeds`.
- `transcribe: true` sends a video without subtitles, or a direct audio or video file, to a new ASR worker (faster-whisper on CPU, or an external OpenAI-compatible transcription endpoint): always a job. An uploaded audio or video file is always transcribed.

**Architecture:**
- **Detect before the fetch.** A URL of a supported platform (`media/sites.py`, our own patterns) skips the page fetch and goes to the media steps; any other URL is fetched as today, and a body that sniffs as audio or video (`media/sniff.py`) is a media file.
- **Subtitles inline, in children (as package 5's D1).** youtube-transcript-api and yt-dlp run in child processes of the pod that holds the request (`MediaPool`, the package-5 `DocumentPool` with another target), killed at the deadline. The parent never imports them. Their only way out is an `EgressProxy` of the parent (the package-4 SSRF proxy) with a byte cap per call.
- **ASR as a pool.** Transcription goes to pool `asr` of the package-3 fair queue, like OCR goes to `docling`: the metadata result is the item's draft, an upload's bytes are its input, the ASR worker downloads URL media itself. One pipeline builds the result (`pipeline_media.py`, like `pipeline_documents.py`).

**Tech Stack:** existing, plus:
- API image: `yt-dlp` (Unlicense; extra `default` without `deno`), `youtube-transcript-api` (MIT).
- ASR image only (extra `asr`): `faster-whisper` (MIT) with `ctranslate2` (MIT), `av` (PyAV, BSD-3; its wheels bundle an LGPL FFmpeg), `yt-dlp[deno]` plus `yt-dlp-ejs` pinned explicitly (Deno, MIT, with the EJS challenge scripts solves YouTube's player challenges for audio downloads; never the `default` extra, it pulls in GPL `mutagen`; Task 9 checks the `yt-dlp-ejs` licence), `openai` (Apache-2.0) for the external backend. Whisper models (MIT) on a PVC, never in the image. No Debian `ffmpeg` (built with `--enable-gpl`).

**Spec:**
- `docs/architecture.md`: Overview (asr-worker, model volume), pipeline step 5 ("Video/audio"), feature note "Audio/video" (tiers, `transcribe: true` always a job, `transcript_source`), error codes `no_transcript_available`, `media_too_long`, `transcription_failed`, "Jobs and the fair queue" (asr pool reuses it), "Documents and OCR" (F3 children, F12 inputs and budget, F14 worker pattern, F15 models).
- `docs/packages.md` row 6: done when YouTube and Mediathek samples return subtitles and an upload is transcribed.
- `CLAUDE.md` rules 2 (stateless, models from a PVC, Helm toggles, jobs over ~30 s), 3 (key from a request header wins), 4, 5 (SSRF on every fetch incl. redirects and sub-requests), 6 (honest user agent, no IP rotation), 7 (no GPL/AGPL).
- Jan's decisions D1-D4 below (2026-10-25 19:04 UTC: the recommended options; plan approved).

## Global Constraints

- English only. Python 3.13. No GPL or AGPL dependency (the licence gate of package 5 covers the new extra `asr`); PO-token provider plugins for yt-dlp (`bgutil-ytdlp-pot-provider`, GPL-3.0) are excluded.
- New dependencies are pinned to their newest release at least 14 days old at implementation time: today `yt-dlp==2026.8.19`, `youtube-transcript-api==1.2.4`, `faster-whisper==1.2.1`, `ctranslate2==4.8.2`, `av==19.0.1`, `deno==2.9.7`. Exception (M16): when Task 1 shows the pinned yt-dlp failing on YouTube or a Mediathek where a newer stable release works, the newest stable release is pinned and the PR says why.
- The API image must not contain faster-whisper, ctranslate2, PyAV, Deno or models. The parent processes of API pods and workers must not import `yt_dlp`, `youtube_transcript_api`, `faster_whisper`, `ctranslate2` or `av` (only children do).
- Every request a media library makes leaves through the `EgressProxy` (SSRF check per connection, byte cap per call). The only exception is the operator-configured external ASR endpoint.
- Stateless pods: inputs, drafts and results live in Valkey with TTLs. Downloaded media lives in a scratch directory (`ASR_SCRATCH_PATH`, an emptyDir) only for the item's run and is deleted in a `finally`. Models live on the PVC, mounted read-only.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it; Helm sets it where the chart owns it. `ASR_EXTERNAL_API_KEY` is a secret.
- The answers for pages, documents and uploads of documents must not change, except: a URL of a supported platform answers with the media result (M1), an HTML page with a `<track>` gains the transcript section and `data.media` (M8), and a body that sniffs as audio or video is a media file instead of `unsupported_content_type`.
- Never log URLs, video ids, query strings, file names or transcript text. Platform, kind, duration, host and `path_hash` at most.
- Not in this package: transcripts of players embedded in a page (D4), speaker diarisation, translation, timestamps in the content, HLS or DASH URLs outside the platform extractors, S3 offload of inputs (package 11), PII in transcripts (package 7 applies to them later like to any content).

## Decisions of Jan (2026-10-25: the recommended options)

- **D1, YouTube refuses the service.** YouTube blocks many data-centre IPs ("confirm you're not a bot") and asks for a proof-of-origin token on a growing share of caption tracks (youtube-transcript-api 1.2.4 then raises `PoTokenRequired`). From a cluster this can hit most YouTube requests.
  - (a, recommended, **chosen**) Answer honestly: tier 2 (title, description, licence from the page data) with `transcript_source: metadata_only`, the warning `subtitles_blocked`, the gate's backoff on 429. No proxy.
  - (b) Like (a), plus an operator may set one fixed upstream proxy for media requests (`MEDIA_UPSTREAM_PROXY`, e.g. an office or residential line), chained behind our SSRF proxy; never rotating.
  - (c) Like (a), plus a later browser fallback (the browser worker opens the transcript panel of the watch page), as its own small package after this one.
- **D2, default Whisper model.** CPU only (int8). Jan re-confirmed large-v3-turbo on 2026-10-25 after the spike (RTF 0.88 on 4 threads).
  - (a, recommended, **chosen**) `large-v3-turbo`: clearly better German, ~1.6 GB on the PVC, ~2.5 GB RAM per child; Task 1 measures its speed on 4 threads. If it is slower than real time there, Task 1 reports it and the default falls to `small`.
  - (b) `small`: ~0.5 GB, roughly 2-3 times faster on CPU, more errors in German.
- **D3, uploads of audio and video.** Upload bytes travel through Valkey (inputs, as for OCR).
  - (a, recommended, **chosen**) Up to `MEDIA_UPLOAD_MAX_BYTES` (25,000,000; about 25 min of MP3 at 128 kbit/s, hours of Opus), counted in the shared input budget. Larger media by URL; S3 lifts the limit in package 11. Jan: the limit must be adjustable: it is an env var and a Helm value (`workers.asr.uploadMaxBytes`), bounded only by the input budget (`DOCLING_INPUT_BUDGET_BYTES`, checked at start-up).
  - (b) Up to 200 MB: the API pod extracts the audio track (16 kHz mono Opus, about 11 MB per hour) in a child before storing it; costs API CPU and a PyAV in the API image.
- **D4, players embedded in a page** (a YouTube iframe on a teaching page, a podcast page's MP3).
  - (a, recommended, **chosen**) Listed in `data.media.embeds` (platform URL or file URL, kind), so the caller can extract them with their own request; only `<track>` text is added inline.
  - (b) Also fetch the subtitles of the first embedded platform player and add them to the page (one more child call per page with an embed, about 1-3 s).
- **D6, length limits for transcription** (asked 2026-10-25 after Task 9: 3 h media against a 1 h ASR timeout at RTF 0.88).
  - (a, recommended, **chosen**) `MEDIA_MAX_DURATION_SECONDS` 7200 (2 h), `ASR_TIMEOUT_SECONDS` 10800 (3 h), `ASR_INPUT_TTL_SECONDS` 28800 (8 h, longer than the 2 × timeout + 10 s lease). All stay adjustable via env and Helm.
  - (b) 1 h media, 90 min timeout.
  - (c) 3 h media, `small` for anything over 1 h, 3 h timeout.

## Rulings in this plan

- **M1, media targets** (`media/sites.py`). A platform URL is recognised by host and path before any fetch, and only in `mode` `fast` or `auto` without browser options (`mode: browser`, actions, cookies, headers, screenshot render the page as today):

  | Platform | URLs | yt-dlp extractor |
  | --- | --- | --- |
  | `youtube` | `youtube.com/watch?v=`, `/shorts/`, `/embed/`, `/live/`, `m.` and `music.` hosts, `youtu.be/<id>`, `youtube-nocookie.com/embed/` | `Youtube` |
  | `vimeo` | `vimeo.com/<digits>`, `player.vimeo.com/video/<digits>` | `Vimeo` |
  | `ard` | `ardmediathek.de/video/...` | `ARDBetaMediathek` |
  | `zdf` | `zdf.de/...` pages with a video (the extractor decides) | `ZDF` |
  | `arte` | `arte.tv/<lang>/videos/...` | `ArteTV` |
  | `3sat` | `3sat.de/...html` | `3sat` (verify the name in the pinned version) |

  Task 1 confirms or trims this list. The extractor is passed as yt-dlp's `allowed_extractors`, so the generic extractor (which follows anything) never runs. The URL is normalised to its canonical watch URL (`https://www.youtube.com/watch?v=<id>`) for the cache key, the fingerprint and the compliance check; `url` in the result stays the request's. A playlist URL without a video id is `unsupported_content_type` ("Extract the videos one by one."); a live stream that is still running is `unsupported_content_type` too. A media file is a fetched or uploaded body whose bytes say MP3 (ID3 or frame sync), MP4/M4A/MOV (`ftyp`), WebM/Matroska (EBML), Ogg, WAV, FLAC or ADTS AAC (`MediaKind`); a fetched response declared `audio/*`, `video/*` or `application/ogg` is read only to its first 64 KiB (`truncate`), so a large file is sniffed, not refused as `too_large`.
- **M2, tiers and selection** (`media/select.py`, pure). Tier 1, subtitles: YouTube asks youtube-transcript-api first (list, then fetch the chosen track) and yt-dlp when it fails; the other platforms ask yt-dlp. The track is chosen in this order: manual tracks in the order of `languages`, automatic captions in that order, a manual track in the video's original language, any manual track (the platform's first), automatic captions in the original language; machine-translated tracks never. Tier 2, metadata: title, description, uploader, upload date, duration, chapters, tags and licence from yt-dlp's info (YouTube from youtube-transcript-api's failure falls through to yt-dlp's info; when both fail, the watch page's `ytInitialPlayerResponse` via the fast fetcher). Tier 3, ASR: only with `transcribe: true` and no subtitles (M9).
- **M3, children** (`media/pool.py`, `media/child.py`). `MediaPool` is `DocumentPool(target="text_extraction.media.child")` with `MEDIA_CONCURRENCY` (2) children and `MEDIA_MEMORY_LIMIT_MB` (1024), started before `/ready` in API pods, extract workers and the browser worker (as the document pool). Child functions: `youtube_transcript`, `ytdlp_info` (info without download, with the chosen subtitle track's bytes), each taking the proxy URL with the call's credentials, the user agent, `languages` and the deadline. The parent's deadline is what the run has left of `EXTRACT_TIMEOUT_SECONDS`; at the deadline the child is killed and the run answers `timeout`, retryable. A child returns plain dicts (only the fields M2 and M6 use; yt-dlp's format list never leaves the child), at most `MEDIA_SUBTITLE_MAX_BYTES` (5,000,000) of track bytes.
- **M4, network.** API pods and workers own one `EgressProxy` (`browser_proxy.py`, unchanged class) for media, started with the media pool; each child call gets its own `ProxyContext` with `MEDIA_EXTRACT_MAX_BYTES` (30,000,000). yt-dlp options: `proxy`, `socket_timeout` 10, `allowed_extractors`, `noplaylist`, `skip_download`, `cachedir: False`, no cookies, `extractor_retries: 0`, our `User-Agent` for page requests (the platform APIs get the client headers their protocol requires, as a player sends them). Deno is not in the API image: tier 1 runs without a JavaScript runtime (Task 1 checks that subtitles still come). The external ASR endpoint is the operator's and is called directly.
- **M5, compliance and outbound.** The canonical media URL passes pipeline step 4 (robots.txt, TDM, ai.txt) like a page, and the run holds the host's turn for the child calls (as a render holds its turn, B3). The requests the libraries make (player APIs, caption files, media segments) are the player's sub-requests: SSRF-checked and byte-capped by the proxy, not robots-checked and not gated, as a browser's sub-requests are. A media file fetched by URL gets the header checks of a document (F10). `outbound.lease_ttl_seconds` covers `MEDIA_DOWNLOAD_TIMEOUT_SECONDS` (M10).
- **M6, the result.** `ExtractResult.media: MediaInfo | None = None` with `platform` (M1, `file`, `upload` or `html5`), `transcript_source` (`manual_subtitles`, `auto_subtitles`, `asr`, `metadata_only`), `transcript_language`, `available_languages` (manual tracks), `duration_seconds`, `uploader`, `asr_model` (ASR only), `embeds: list[MediaEmbed]` (D4: `url`, `kind` `platform|file`, `platform`), at most 10. `mode_used` is `fast` (`upload` for uploads); `content_type` is the media file's type, `text/vtt` for a subtitle result, `text/html` for `metadata_only`. `content` is the transcript as paragraphs (a new paragraph after a gap of 2 s or more, or after 6 sentences), under `# <title>`, with `## <chapter>` headings where the platform gives chapters; `text` drops the headings' markers, `html` is `documents.formats.from_markdown`. `metadata_only` content is the title and description. The description, author, date and licence go to `metadata` (`PageMetadata`; a licence with the new source `platform`, e.g. YouTube's "Creative Commons Attribution license (reuse allowed)" → `CC-BY-3.0`, confidence `high`). Quality is judged like `text/plain`; `metadata_only` is `partial` with `error.code` `no_transcript_available` (not retryable; the message names `transcribe: true` when the service has ASR). New warnings: `subtitles_blocked` (D1), `asr_unavailable` (no transcription in time or the ASR failed: the draft answers), `tracks_truncated` (more tracks than `MEDIA_MAX_TRACKS`).
- **M7, captions** (`media/captions.py`). WebVTT (cue settings, `NOTE`, `STYLE`, `REGION` blocks, voice and class spans), SRT, TTML (defusedxml, any namespace prefix, `<br/>`, `<span>` styling) and YouTube `json3` become cues `(start, end, text)`; tags and entities are removed, a byte order mark and a non-UTF-8 charset (charset-normalizer) are handled, and the rolling duplicates of automatic captions are dropped (a cue's leading lines that repeat the previous cue's last lines). `media/transcript.py` builds M6's paragraphs from cues or ASR segments.
- **M8, HTML5 tracks and embeds** (`media/html5.py`). After an HTML page's conversion (fast or rendered, `success` or `partial`), its `<video>` and `<audio>` elements with `<track src>` of kind `subtitles`, `captions` or none give up to `MEDIA_MAX_TRACKS` (3) tracks, one per element (by `languages`, then the page language, then `default`, then the first). Each is fetched with the fast `Fetcher` in a turn of its host (SSRF and gate as for any fetch; no compliance files: it is a sub-resource of a page that passed), at most `MEDIA_SUBTITLE_MAX_BYTES`, and its text is appended as `## Transcript` (one per track, with its label); `media.platform` `html5`, `transcript_source` `manual_subtitles`. A track that fails is skipped silently (a warning only when every one failed: `tracks_failed`). Embeds (D4 a): iframes of M1's platforms (normalised), `<video>`/`<audio>`/`<source>` with an http(s) `src`, JSON-LD `VideoObject`/`AudioObject` (`contentUrl`, `embedUrl`), `og:video`, `og:audio`. A page with neither tracks nor embeds has `media: null`.
- **M9, `transcribe` and ASR dispatch.** `ExtractRequest`, `BatchRequest` and `JobOptions` gain `transcribe: bool = False` and `languages: list[LanguageCode] = []` (at most 5, `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`, lowercased primary tag; stored in a job only when not default, as `ocr`). `transcribe: true` while the service has no ASR (`ASR_ENABLED` and `JOBS_ENABLED`) is 400 `invalid_request` ("Transcription is not enabled on this service."). `transcribe: true` without `wait_seconds` defaults `wait_seconds` to 20, so a video with subtitles answers 200 as usual; when ASR is needed (no subtitles, or a media file) the `AsrHandoff` creates a one-item job (kind `extract`) on pool `asr` with the metadata result as its draft and the `MediaInputMeta` as its input (empty body), claims the `Handover` and the route answers 202 at once: transcription is always a job. A media file without `transcribe` is `failed`/`no_transcript_available` ("Send transcribe: true to transcribe it."). Back-pressure as F12: memory guard high, `ASR_MAX_OPEN_PER_CLIENT` (3) open ASR jobs, or the input budget full → the draft answers with `asr_unavailable` (a media file without draft: `failed`/`rate_limited`, retryable). Batches: the extract worker moves an item that needs ASR to pool `asr` (input, draft, queue, completion), as F13. The input budget (`DOCLING_INPUT_BUDGET_BYTES`) becomes the budget of all inputs; its name stays, its documentation says so. Cache: the key gains `media` for platform URLs (so cached page results of a YouTube URL are not served after the deploy) and `languages` when set; a cached `metadata_only` result is no hit for `transcribe: true`. The fingerprint variant of a media result is `<format>|media`.
- **M10, the ASR worker** (`asr_worker.py`, entry `text-extraction-asr-worker`, pool `asr`, internal port only). The package-3 `Worker` with an `AsrRunner`; leases of `2 × ASR_TIMEOUT_SECONDS + 10`. `ASR_CONCURRENCY` (1) slot loops, each with a child (`DocumentPool(target="text_extraction.media.asr_child")`, `ASR_MEMORY_LIMIT_MB` 4096) that loads the model once. Per item: (1) the media: an upload's input bytes, written to `ASR_SCRATCH_PATH`; a platform URL downloaded by yt-dlp in the child (format `bestaudio/best[height<=360]`, at most `MEDIA_MAX_BYTES` 500,000,000, `MEDIA_DOWNLOAD_TIMEOUT_SECONDS` 600) through the worker's `EgressProxy`, in the page host's turn for the info request only; a media file URL downloaded by the child through the proxy in the file host's turn; (2) the duration (yt-dlp's, else PyAV's probe) over `MEDIA_MAX_DURATION_SECONDS` (10,800) is `media_too_long` before the transcription; (3) PyAV decodes the first audio stream to 16 kHz mono float32; faster-whisper `WhisperModel(<ASR_MODELS_PATH>/<ASR_MODEL>, device="cpu", compute_type=ASR_COMPUTE_TYPE (int8), cpu_threads=ASR_THREADS (4))`, `vad_filter=True`, `condition_on_previous_text=False`, `language` the first of `languages` or detected; (4) `Pipeline.transcribed_result(req, principal, meta, draft, transcription)` builds M6's result (the draft's metadata, `transcript_source: asr`, `asr_model`) and, for a URL, compares and stores it like a fast result (`_finish`). The scratch file is deleted in a `finally`. The ASR timeout is not retryable (the same file would time out again), a failed download is retryable, a decoder error is `transcription_failed` (not retryable), a missing model `internal_error` (retryable). The draft answers with `asr_unavailable` when the ASR failed, and loses its fingerprint when the failure is retryable (F14's rule). Before `/ready` the worker checks the models (local backend) and transcribes the built-in clip (`media/asr_check.ogg`) in each child; a failure exits 1 naming `ASR_MODELS_PATH`. SIGTERM: stop claiming, 30 s for items in flight, then kill the children; their leases expire and the items run again.
- **M11, external ASR** (`media/asr_external.py`). `ASR_BACKEND` `local` (default) or `openai`. The child decodes as M10, encodes 16 kHz mono Opus at 32 kbit/s in Ogg (PyAV; Task 1 confirms the encoder is in the wheel, else MP3 at 48 kbit/s), splits it into chunks of at most 20 minutes, and posts each to `ASR_EXTERNAL_BASE_URL` `/audio/transcriptions` (OpenAI SDK, `ASR_EXTERNAL_MODEL` `whisper-1`, `response_format` `verbose_json`, falling back to `json`), offsetting the segments. Key: the request header `X-ASR-Api-Key` wins (stored as the item's caller secret, deleted with its outcome, never logged), else `ASR_EXTERNAL_API_KEY` (secret); without either, `transcribe: true` is 400 while the backend is `openai`. 401/403 → `transcription_failed`, not retryable; 429, 5xx and timeouts (`ASR_EXTERNAL_TIMEOUT_SECONDS`, 300, per chunk) → `transcription_failed`, retryable. The worker image is the same; with the external backend it needs no models (the model check and the self-check's local part are skipped).
- **M12, switches.** `MEDIA_ENABLED` (default true: platform URLs, media files, tracks, embeds). `ASR_ENABLED` (default false, as D3 of package 5): Helm renders it into every pod from `workers.asr.enabled` (default false); compose profile `asr` with `ASR_ENABLED=true` in `.env`.
- **M13, models on the PVC** (`media/models.toml`, the package-5 `documents/model_store.py` made manifest-generic). `--download-models` fills `ASR_MODELS_PATH/<model>` from Hugging Face at a pinned commit per file (`model.bin`, `config.json`, `tokenizer.json`, `vocabulary.*`, `preprocessor_config.json`) with size and SHA-256 (values from Task 1), staging directory, rename, `.complete-<manifest hash>`; only the configured `ASR_MODEL` is downloaded; `--check` transcribes the built-in clip. Helm: init container with the PVC writable, the worker read-only, `models.download: false` + `existingClaim` for air-gapped volumes. Compose: one-shot `asr-models` on the volume `asr-models`.
- **M14, uploads** (`file_api.py`). A file part that sniffs as audio or video is transcribed (no form field needed): bytes up to `MEDIA_UPLOAD_MAX_BYTES` (D3) as the item's input (`MediaInputMeta`, TTL `ASR_INPUT_TTL_SECONDS` 21,600, never longer than the job), job kind `file` on pool `asr`, 202 at once (the form's `wait_seconds` does not apply to ASR). The body limit of the path becomes `max(DOCUMENT_MAX_BYTES, MEDIA_UPLOAD_MAX_BYTES)` + 64 KiB; a document over `DOCUMENT_MAX_BYTES` still answers `too_large`. While the service has no ASR, an audio upload answers `failed`/`unsupported_content_type` ("Transcription is not enabled on this service.").
- **M15, metrics.** `text_extraction_media_extractions_total{platform, source, outcome}`, `text_extraction_media_seconds{platform}` (API pods and workers); `text_extraction_asr_dispatch_total{outcome}` (as Docling's outcomes); `text_extraction_asr_transcriptions_total{backend, outcome}`, `text_extraction_asr_audio_seconds_total{backend}` (the cost basis), `text_extraction_asr_seconds{backend}` (ASR worker); queue gauges for pool `asr`. Helm alerts: `TextExtractionQueueBacklog` for pool `asr` (`alerts.asrQueueBacklogItems`, 50), `TextExtractionWorkersDown` for the ASR workers.
- **M17, volumes (Jan, 2026-10-25: keep the main volume small).** No model or downloaded media in an image, on the root filesystem or on a shared data volume. Each worker type with models has its own model PVC (`docling-models`, `asr-models`; 4Gi for `large-v3-turbo` plus a staged copy during an update), mounted read-only in its workers; only the configured `ASR_MODEL` is downloaded. Downloaded media lives on a per-pod emptyDir with `sizeLimit` (`workers.asr.scratch.sizeLimit`, 2Gi; `MEDIA_MAX_BYTES` must fit it, checked by the chart). Model updates are a data task of their own: `models.downloadMode: init` (default, works with ReadWriteOnce: the init container fills the volume when the manifest changed) or `job` (a Helm post-install/post-upgrade hook Job fills the volume once per release; worker pods then only check the marker and start fast; needs ReadWriteMany or one node). The same switch applies to the Docling models. No sidecar: the models are read once when a child starts, so a permanently running updater would only cost resources.
- **M18, spike findings (Task 1, 2026-10-25; `docs/superpowers/plans/2026-10-25-package-6-spike.md`).**
  - yt-dlp's `allowed_extractors` matches `IE_NAME` (case-insensitive full match); an unknown name silently loads nothing. The names are `youtube`, `vimeo`, `ardmediathek`, `zdf`, `artetv`, `3sat`, kept in `media/sites.py` with a unit test that each matches exactly one extractor of the pinned yt-dlp. ZDF also takes the new `zdf.de/video/<section>/<show>/<episode>` URLs.
  - Vimeo goes to yt-dlp as `https://player.vimeo.com/video/<digits>` (works without login; `vimeo.com/<id>` needs one). An embed-restricted video (player 403) answers tier 2 from the fast fetch of the `vimeo.com` page. *Live run 2026-10-26: `vimeo.com/<id>` of such videos is a bot check and oEmbed 404s, so tier 2 reads the player page's `og:` tags instead, and a player 403/404/410 answers `unavailable`, not retryable.* No impersonation (`curl_cffi`), which would break the honest user agent.
  - Track languages compare by primary subtag after normalisation: ISO 639-2 codes (`deu`, `eng`, ...) map to 639-1; named keys (`de-XwLwiJMB_Xs`) count as `de`; of YouTube's automatic captions only `<lang>-orig` (else the original language's own track) is taken, every other automatic language is a machine translation; arte's `-forced` tracks and `und` never count as a transcript.
  - arte and Vimeo deliver subtitles as HLS (`m3u8_native`): the child reads the playlist (`#EXTM3U`) and fetches and concatenates its VTT segments, within the byte cap and through the proxy.
  - faster-whisper 1.2.1's `decode_audio` breaks on PyAV 19 (`metadata_errors`): the child always decodes itself and passes a float32 array, never a path (a test pins it).
  - Deno is not needed for tier 1, and audio download worked without it from Jan's line; it stays in the ASR image only, for YouTube's player challenges from other networks.
  - PyAV wheels bundle GPL `libx264`/`libx265`: decision D5: Jan chose (a) on 2026-10-25, build PyAV from source in the ASR image against a pinned BtbN LGPL-shared FFmpeg build (no GPL x264/x265); Task 9 adds a test that the installed `av` reports an LGPL FFmpeg configuration.
- **M16, yt-dlp versions.** The 14-day rule holds; a pinned yt-dlp that a platform broke is replaced by the newest stable release in its own small PR (CONTRIBUTING.md gains this exception, as the platforms change their players without notice).

## Review Focus

1. **YouTube refuses** (bot check, sign-in, `PoTokenRequired`, 429, an empty 200 caption body): the answer is `metadata_only` with `subtitles_blocked` (D1 a), never `internal_error`; a 429 reaches the gate's backoff; no retry storm across tiers (each tier at most once per request). Owner: Tasks 5 and 6.
2. **Huge or endless media:** a 10-hour video, a running live stream, a 2 GB file URL, an upload that claims to be MP3 but is random bytes, a file whose duration header lies. `media_too_long`, `unsupported_content_type`, `too_large` or `transcription_failed`; the child is killed at its limit, the scratch file is gone, the next item runs. Owner: Tasks 3, 9.
3. **SSRF through media:** a platform redirect to `169.254.169.254`, a `<track src="http://127.0.0.1/...">`, an embed pointing at a private host, a media file URL that redirects to Valkey's port. The proxy or the fetcher refuses; the track is skipped, the download fails `blocked_url`. Owner: Tasks 5, 6, 9.
4. **Caption oddities:** rolling duplicates of YouTube automatic captions, TTML with namespaces and styling, WebVTT with `NOTE`/`STYLE` blocks and voice spans, a Latin-1 SRT, an empty track, a track of 50 MB. Clean text without repeated lines, or the next tier. Owner: Task 4.
5. **ASR backlog and lost work:** no ASR worker (scaled to zero, models missing), an upload whose input expires in the queue, SIGTERM in the middle of a one-hour transcription, the external endpoint answering 429. Jobs wait and the backlog alert fires; an expired input answers the draft with `asr_unavailable` (an upload without draft: `timeout`, retryable); a killed item runs again; 429 is retryable. Owner: Tasks 8, 9, 10.

---

### Task 1: Spike on Jan's PC and model pins

Runs through Remote Control in a fresh clone folder (`C:\Users\jan\te-pkg6-verify`), never in the staging folder; the sandbox reaches neither YouTube, the Mediatheken nor Hugging Face.

**Files:**
- Create: `docs/superpowers/plans/2026-10-25-package-6-spike.md` (results), `tests/live/urls/media.csv` (url, platform, expected_source, expected_lang, notes: at least 4 YouTube videos with manual German subtitles, 2 with automatic captions only, 2 Vimeo, 3 ARD Mediathek, 3 ZDF, 1 arte, 1 3sat, 2 direct MP3/MP4 files, 2 pages with `<track>`), `tests/media/corpus/speech_de.ogg` (30 s of a public-domain LibriVox German reading, 16 kHz mono Opus) with `speech_de.expected.txt` and `tests/media/corpus/SOURCES.md`, `src/text_extraction/media/asr_check.ogg` (its first 5 s)
- Modify: none

- [ ] **Step 1:** in a scratch venv on Jan's PC: youtube-transcript-api 1.2.4 and yt-dlp (pinned, without Deno, our user agent) against the media set: per URL tier 1 found yes/no, error type, seconds. Repeat yt-dlp with Deno for the YouTube rows.
- [ ] **Step 2:** faster-whisper 1.2.1 on `speech_de.ogg` and a 5-minute German clip, `small` and `large-v3-turbo`, int8, 4 threads: wall time, real-time factor, peak RSS, word recall against the expected text.
- [ ] **Step 3:** download both models at a pinned Hugging Face commit, record each file's URL, size and SHA-256 for `media/models.toml`; check that the PyAV wheel's FFmpeg is LGPL (`av.library_versions`, configuration string without `--enable-gpl`) and has the `libopus` encoder.
- [ ] **Step 4:** write the results file. If tier 1 fails for most YouTube rows even from Jan's line, stop and bring Jan a decision before Task 5. Commit `docs: package 6 spike results and media sample set`.

### Task 2: Settings, models and codes

**Files:**
- Modify: `src/text_extraction/settings.py`, `src/text_extraction/extract_models.py`, `src/text_extraction/job_models.py`, `src/text_extraction/pool_dispatch.py` (`job_options`), `src/text_extraction/licenses.py` (source `platform`), `docs/configuration.md` (section "Audio and video"), `.env.example`
- Create: `src/text_extraction/media/__init__.py`, `src/text_extraction/media/types.py`
- Test: `tests/test_media_models.py`, `tests/test_settings.py` (extend)

**Interfaces:**
- Produces: settings `media_enabled` (True), `media_concurrency` (2, ≥ 1), `media_memory_limit_mb` (1024, ≥ 256), `media_extract_max_bytes` (30_000_000), `media_subtitle_max_bytes` (5_000_000), `media_max_tracks` (3, 0-10), `media_max_duration_seconds` (10_800, ≥ 60), `media_max_bytes` (500_000_000), `media_upload_max_bytes` (25_000_000, ≥ 1_000_000, ≤ `docling_input_budget_bytes`), `media_download_timeout_seconds` (600, 30-3600), `asr_enabled` (False), `asr_backend` (`"local" | "openai"`), `asr_model` ("large-v3-turbo", one of the manifest's names), `asr_models_path` (`/models`), `asr_compute_type` ("int8"), `asr_threads` (4), `asr_concurrency` (1), `asr_memory_limit_mb` (4096), `asr_timeout_seconds` (3600, 60-14_400), `asr_input_ttl_seconds` (21_600, ≥ 600), `asr_max_open_per_client` (3), `asr_scratch_path` (`/tmp/asr`), `asr_external_base_url` (None, https only), `asr_external_model` ("whisper-1"), `asr_external_api_key` (secret, None), `asr_external_timeout_seconds` (300).
- Produces: `MediaKind = Literal["mp3","mp4","m4a","mov","webm","mkv","ogg","wav","flac","aac"]`; `Platform = Literal["youtube","vimeo","ard","zdf","arte","3sat","file","upload","html5"]`; `TranscriptSource = Literal["manual_subtitles","auto_subtitles","asr","metadata_only"]`; `MediaEmbed(url, kind: Literal["platform","file"], platform: Platform | None)`; `MediaInfo` (M6 fields); `ExtractResult.media: MediaInfo | None = None`; `LanguageCode` (annotated str, M9 pattern); `transcribe` and `languages` on `ExtractRequest`, `BatchRequest`, `JobOptions`.

- [ ] **Step 1: Failing tests:** `test_cached_result_without_media_still_parses` (a package-5 `ExtractResult` JSON validates, `media is None`), `test_languages_validated` (`["de","en-GB"]` ok and lowercased primary tag; `["deutsch"]`, six entries → 422), `test_job_options_omit_default_media_fields` (dump of default `JobOptions` has neither `transcribe` nor `languages`), `test_asr_external_base_url_https_only`, `test_media_upload_max_bytes_bounded`, `test_asr_external_api_key_not_in_repr`.
- [ ] **Step 2:** run, expect FAIL. **Step 3:** implement, with the `docs/configuration.md` rows (secret marked) and `.env.example`. **Step 4:** PASS, `uv run mypy` clean.
- [ ] **Step 5:** commit `feat: add audio and video settings and result models`.

### Task 3: Media targets and sniffing

**Files:**
- Create: `src/text_extraction/media/sites.py`, `src/text_extraction/media/sniff.py`
- Modify: `src/text_extraction/fetch.py` (keep the first 64 KiB of a declared media type)
- Test: `tests/test_media_sites.py`, `tests/test_media_sniff.py`, `tests/test_fetch.py` (extend)

**Interfaces:**
- Produces: `MediaTarget(platform: Platform, canonical_url: str, extractor: str, video_id: str | None)`; `media_target(url: str) -> MediaTarget | None`; `sniff_media(head: bytes, declared: str | None) -> MediaKind | None`; `media_type_of(kind: MediaKind) -> str`; `is_declared_media(content_type: str | None) -> bool`; `MEDIA_HEAD_BYTES = 65_536`.

- [ ] **Step 1: Failing tests:** `test_youtube_url_forms` (watch, shorts, embed, live, `youtu.be`, `m.` host, `youtube-nocookie`, extra query parameters, `&t=` kept out of the canonical URL → canonical `https://www.youtube.com/watch?v=<id>`), `test_youtube_playlist_without_video_is_none_target_with_flag` (returns a target whose `video_id` is None, so the pipeline answers M1's message), `test_youtube_lookalike_hosts_refused` (`youtube.com.evil.example`, `evilyoutube.com` → None), `test_vimeo_ard_zdf_arte_3sat_forms`, `test_non_media_url_none`, `test_sniff_kinds` (byte headers for each `MediaKind`; random bytes and a PDF → None; `ftyp` brand `M4A ` → m4a, `qt  ` → mov), `test_fetch_keeps_head_of_declared_media` (pytest-httpserver serves 50 MB `audio/mpeg` with Content-Length → `FetchResult.truncated` and 64 KiB, no `too_large`).
- [ ] **Step 2:** FAIL. **Step 3:** implement (host match on the registrable suffix, no regex on the whole URL). **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: recognise media platforms and sniff audio and video files`.

### Task 4: Captions and transcripts

**Files:**
- Create: `src/text_extraction/media/captions.py`, `src/text_extraction/media/transcript.py`, fixtures `tests/media/captions/` (`youtube_auto.vtt` with rolling duplicates, `manual.vtt` with `NOTE`/`STYLE` and voice spans, `latin1.srt`, `ard.ttml`, `zdf.vtt`, `youtube.json3`, `empty.vtt`)
- Test: `tests/test_media_captions.py`, `tests/test_media_transcript.py`

**Interfaces:**
- Produces: `Cue(start: float, end: float, text: str)`; `parse_captions(body: bytes, fmt: Literal["vtt","srt","ttml","json3"], charset: str | None = None) -> list[Cue]` (raises `CaptionError` on an unreadable track); `Chapter(start: float, title: str)`; `build_transcript(cues: Sequence[Cue], title: str | None, chapters: Sequence[Chapter], fmt: OutputFormat) -> str` (M6 paragraphs).

- [ ] **Step 1: Failing tests:** `test_rolling_duplicates_removed` (each spoken line once), `test_vtt_blocks_and_spans_dropped`, `test_srt_latin1` ("Größe" decoded), `test_ttml_namespaces_and_br`, `test_json3_segments`, `test_empty_track_gives_no_cues`, `test_entities_and_tags_removed`, `test_paragraph_breaks` (gap ≥ 2 s → new paragraph; 7 sentences without gap → break after 6), `test_chapters_as_headings`, `test_text_and_html_formats` (`text` has no `#`; `html` has `<h1>` and escapes `<script>` in a cue), `test_ttml_external_entity_refused` (defusedxml: no expansion, `CaptionError`).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: parse WebVTT, SRT, TTML and json3 captions into transcripts`.

### Task 5: Subtitles and metadata in children

**Files:**
- Create: `src/text_extraction/media/pool.py`, `src/text_extraction/media/child.py`, `src/text_extraction/media/select.py`
- Modify: `pyproject.toml`, `uv.lock` (`yt-dlp` without extras, since `default` pulls in GPL `mutagen`; `youtube-transcript-api`), `src/text_extraction/app.py`, `src/text_extraction/worker.py`, `src/text_extraction/browser_worker.py` (start the media pool and its `EgressProxy` before `/ready`, stop them at shutdown)
- Test: `tests/test_media_select.py`, `tests/test_media_child.py`

**Interfaces:**
- Consumes: `DocumentPool` (F3), `EgressProxy`/`ProxyContext` (package 4), `parse_captions` (Task 4).
- Produces: `create_media_pool(settings) -> DocumentPool`; `MediaProbe` (dataclass from the child's dict: `title`, `description`, `uploader`, `upload_date`, `duration`, `license`, `chapters`, `tags`, `original_language`, `manual: dict[str, list[TrackRef]]`, `automatic: dict[str, list[TrackRef]]`, `track: FetchedTrack | None`, `is_live`, `blocked: bool`, `error: str | None`); `choose_track(manual, automatic, original_language, languages) -> tuple[str, TranscriptSource] | None` (M2 order, never translated tracks); `MediaFetcher.probe(target: MediaTarget, languages, deadline: float) -> MediaProbe` (tries `youtube_transcript` then `ytdlp_info` for YouTube, `ytdlp_info` otherwise, each child call with its own `ProxyContext`).

- [ ] **Step 1: Failing tests:** `test_choose_track_order` (parametrised: requested manual > requested auto > original manual > any manual > original auto; `de` auto-translated from `en` never), `test_child_never_imported_in_parent` (after a probe through a fake target, `yt_dlp` and `youtube_transcript_api` are not in `sys.modules`), `test_ytdlp_info_through_proxy_with_local_site` (test-only extractor allowlist `["generic"]`, a local page with `<video><track src="de.vtt" srclang="de">`, proxy policy allowing loopback in the test: the track's cues come back, the proxy context saw the requests), `test_proxy_refuses_private_redirect` (same setup, the track redirects to a private address under the production policy → no track, `error` names the refusal, no exception), `test_child_deadline_kills_and_times_out` (fake child function sleeping → `ConversionTimeout` within deadline + 1 s; next probe works), `test_youtube_blocked_maps_to_blocked` (fake `youtube_transcript` raising the library's `RequestBlocked`/`PoTokenRequired`, fake `ytdlp_info` raising "Sign in to confirm" → `MediaProbe.blocked` True, one call each), `test_track_byte_cap` (track over `MEDIA_SUBTITLE_MAX_BYTES` → no track, `error` `too_large`).
- [ ] **Step 2:** FAIL. **Step 3:** implement; yt-dlp options per M4; youtube-transcript-api with a `requests.Session` whose proxies point at the context and whose User-Agent is ours. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: fetch subtitles and metadata with yt-dlp and youtube-transcript-api in child processes`.

### Task 6: Media in the URL pipeline

**Files:**
- Create: `src/text_extraction/pipeline_media.py`, `src/text_extraction/media/html5.py`
- Modify: `src/text_extraction/pipeline.py` (step 2b platform targets, step 7 media files, HTML5 tracks and embeds after the conversion, `_cache_options` and `_fingerprint_variant`, module docstring), `src/text_extraction/metadata.py` (embeds from JSON-LD and OpenGraph), `src/text_extraction/extract_api.py` and `src/text_extraction/jobs_api.py` (400 for `transcribe` while ASR is off; `wait_seconds` default 20 with `transcribe`)
- Test: `tests/test_pipeline_media.py`, `tests/test_media_html5.py`

**Interfaces:**
- Consumes: Tasks 2-5.
- Produces: `MediaSteps(media_fetcher, fetcher, gate, settings, envelope, registry)` with `platform(run, target) -> Envelope | AsrNeeded`, `media_file(run, fetched, kind) -> Envelope | AsrNeeded`, `tracks(run, html, result) -> ExtractResult`; `AsrNeeded(draft: Envelope | None, input: MediaInputMeta)`; `MediaInputMeta(source: Literal["platform","file","upload"], platform, url: str | None, extractor: str | None, kind: MediaKind | None, file_name: str | None, duration: float | None, languages: list[str], compliance: ComplianceReport | None, attempts: int)`; `run.asr_handoff` (same shape as `ocr_handoff`).

- [ ] **Step 1: Failing tests** (fake `MediaFetcher`; pytest-httpserver for files and tracks): `test_youtube_with_manual_subtitles` (status success, `media.transcript_source == "manual_subtitles"`, `content` starts with `# <title>`, licence from the probe as `CC-BY-3.0` with source `platform`, `mode_used == "fast"`, no page fetch), `test_youtube_auto_captions_only`, `test_no_subtitles_metadata_only` (`partial`, `no_transcript_available`, description in content), `test_blocked_gives_subtitles_blocked_warning`, `test_robots_disallow_on_platform_enforced` (fake robots disallowing `/watch` under `enforce` → `blocked_by_robots`, no child call), `test_platform_holds_turn_during_probe`, `test_playlist_and_live_unsupported`, `test_mode_browser_skips_media_path`, `test_cache_key_has_media` (a stored page result of the same YouTube URL is not served), `test_metadata_only_cache_not_hit_for_transcribe`, `test_media_file_without_transcribe` (`failed`/`no_transcript_available`), `test_media_file_with_transcribe_returns_asr_needed`, `test_html5_track_appended` (page with two `<video>` and tracks: `## Transcript` sections, `media.platform == "html5"`), `test_track_to_private_address_skipped`, `test_embeds_listed` (YouTube iframe, `<audio src=…mp3>`, JSON-LD `AudioObject` → three embeds, normalised watch URL), `test_page_answer_unchanged_without_media` (a package-5 HTML fixture gives identical JSON apart from timings), `test_transcribe_400_while_asr_off`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; turn, compliance and cache stay in `pipeline.py`, the media branches are new. **Step 4:** PASS, full suite green.
- [ ] **Step 5:** commit `feat: extract subtitles of platform URLs, media files and HTML5 tracks`.

### Task 7: Audio and video uploads

**Files:**
- Modify: `src/text_extraction/file_api.py`, `src/text_extraction/app.py` (path limit), `src/text_extraction/pipeline.py` (`extract_upload` media branch), `docs/architecture.md` (Public API row)
- Test: `tests/test_file_api.py` (extend)

**Interfaces:**
- Consumes: `sniff_media` (Task 3), `AsrNeeded` (Task 6).
- Produces: `extract_upload` returns `AsrNeeded` (through `asr_handoff`) for media kinds.

- [ ] **Step 1: Failing tests:** `test_audio_upload_while_asr_off` (`failed`/`unsupported_content_type`, message names transcription), `test_audio_upload_over_media_limit` (`too_large`), `test_body_limit_is_max_of_both` (Content-Length of `MEDIA_UPLOAD_MAX_BYTES` + 1 KiB with `DOCUMENT_MAX_BYTES` smaller → not 413 at the middleware; a PDF of that size → `too_large`), `test_audio_upload_hands_to_asr` (fake handoff called with `MediaInputMeta(source="upload", kind="ogg", file_name=…)`).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: accept audio and video uploads for transcription`.

### Task 8: ASR dispatch, inputs and back-pressure

**Files:**
- Create: `src/text_extraction/asr_dispatch.py`
- Modify: `src/text_extraction/extract_api.py`, `src/text_extraction/file_api.py`, `src/text_extraction/worker.py` (move to `asr`, as F13), `src/text_extraction/jobs.py` (open ASR jobs per owner, as Docling's), `src/text_extraction/queue_metrics.py` (pool `asr`, dispatch counter), `src/text_extraction/server.py` (nothing waits for ASR: graceful seconds unchanged)
- Test: `tests/test_asr_dispatch.py`, `tests/test_worker.py` (extend)

**Interfaces:**
- Consumes: `PoolDispatcher`, `Handover`, `JobStore.put_input` (F12), `AsrNeeded` (Task 6).
- Produces: `asr_queue(valkey, settings) -> FairQueue`; `AsrDispatcher(PoolDispatcher)`; `AsrHandoff` (the `asr_handoff` of a sync request: dispatch, claim the `Handover`, route answers 202); `AsrMove` (batch items); `asr_lease_seconds(settings)`; `ASR_OUTCOMES` (`handed_over`, `backend_error`, `memory_guard`, `open_limit`, `input_budget`).

- [ ] **Step 1: Failing tests:** `test_transcribe_without_subtitles_answers_202` (fakeredis; job kind `extract` on pool `asr`, draft stored, input meta stored with empty body), `test_transcribe_with_subtitles_answers_200` (no job), `test_upload_hands_over_202_with_input_bytes` (input TTL `ASR_INPUT_TTL_SECONDS`, budget reserved), `test_open_limit` (three open → fourth answers the draft with `asr_unavailable`; a media file without draft → `rate_limited`, retryable), `test_memory_guard_high`, `test_input_budget_full`, `test_secret_header_stored_and_never_in_job_view` (`X-ASR-Api-Key` stored as the job's secret; `GET /v1/jobs/{id}` has no key), `test_batch_item_moves_to_asr` (order input, draft, queue, completion; a crash after the draft re-runs and moves once), `test_docling_and_browser_dispatch_unchanged` (their suites pass untouched).
- [ ] **Step 2:** FAIL. **Step 3:** implement on `PoolDispatcher`; the ASR handoff does not wait for the result. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: dispatch transcriptions to the asr pool`.

### Task 9: The ASR worker (local backend)

**Files:**
- Create: `src/text_extraction/asr_worker.py`, `src/text_extraction/media/asr_child.py`, `src/text_extraction/media/models.toml`, `src/text_extraction/model_store.py` (moved from `documents/model_store.py`, manifest-generic; the Docling caller unchanged in behaviour)
- Modify: `pyproject.toml` (extra `asr`, script `text-extraction-asr-worker`), `uv.lock`, `src/text_extraction/pipeline.py` (`transcribed_result`), `src/text_extraction/outbound.py` (`lease_ttl_seconds` covers `MEDIA_DOWNLOAD_TIMEOUT_SECONDS`), `NOTICE` (Whisper model licence)
- Test: `tests/test_asr_worker.py` (fakes, default suite), `tests/test_model_store.py` (moved and extended), `tests/asr/test_asr_child.py` (marker `asr`, real model)

**Interfaces:**
- Consumes: `Worker`, `ItemRunner`, `DocumentPool`, `EgressProxy`, `MediaInputMeta`, `build_transcript` (Task 4).
- Produces: `AsrRunner(ItemRunner)`; child functions `"download"` (`{"source", "url", "extractor", "proxy", "user_agent", "max_bytes", "timeout", "scratch"}` → `{"path", "duration", "kind"}`), `"transcribe"` (`{"path", "model_path", "compute_type", "threads", "language", "max_duration"}` → `{"segments": [[start, end, text], ...], "language", "duration"}`); `Transcription(segments: list[Cue], language: str, duration: float, model: str)`; `Pipeline.transcribed_result(req: ExtractRequest | FileForm, principal, meta: MediaInputMeta, draft: Envelope | None, transcription: Transcription | Envelope) -> Envelope`; CLI `--download-models`, `--check`.

- [ ] **Step 1: Failing tests (fakes):** `test_runner_upload_item` (input bytes to scratch, fake transcribe → result `transcript_source == "asr"`, `asr_model`, scratch file deleted), `test_runner_url_item_writes_cache` (and an upload's does not), `test_too_long_before_transcription` (fake download duration 4 h → `media_too_long`, transcribe never called), `test_child_timeout_not_retryable`, `test_download_failure_retryable_draft_answers` (draft with `asr_unavailable`, fingerprint None), `test_expired_upload_input_timeout_retryable`, `test_missing_model_exit_1`, `test_scratch_deleted_on_crash`, `test_worker_parent_imports_nothing_heavy` (`faster_whisper`, `ctranslate2`, `av`, `yt_dlp` not in `sys.modules`), `test_model_store_generic_docling_unchanged` (package-5 model store tests pass against the moved module).
- [ ] **Step 2: Failing tests (marker `asr`):** `test_transcribes_german_clip` (`speech_de.ogg`, the configured model from `ASR_MODELS_PATH`, word recall ≥ 0.8 against `speech_de.expected.txt`), `test_random_bytes_fail_cleanly` (`transcription_failed`), `test_duration_probe`.
- [ ] **Step 3:** FAIL. **Step 4:** implement; verify the faster-whisper and PyAV names against the pinned versions and note them in the module docstring. **Step 5:** PASS (`uv run pytest -m asr` in CI's `media` job and on Jan's PC).
- [ ] **Step 6:** commit `feat: add the asr worker with faster-whisper, models download and self-check`.

### Task 10: External ASR backend

**Files:**
- Create: `src/text_extraction/media/asr_external.py`
- Modify: `src/text_extraction/media/asr_child.py`, `src/text_extraction/asr_worker.py` (backend switch, key order), `pyproject.toml` (`openai` in extra `asr`)
- Test: `tests/test_asr_external.py` (pytest-httpserver as the OpenAI-compatible endpoint)

**Interfaces:**
- Consumes: the `"transcribe"` child function's contract (Task 9).
- Produces: `transcribe_external(path, *, base_url, model, api_key, timeout, chunk_seconds=1200) -> Transcription`; key order header secret > `ASR_EXTERNAL_API_KEY`.

- [ ] **Step 1: Failing tests:** `test_chunks_and_offsets` (a 45-minute fake audio → 3 posts, segments offset by 1200 and 2400 s), `test_verbose_json_fallback_to_json`, `test_header_key_wins` (the endpoint sees the header key; without it the env key), `test_401_not_retryable`, `test_429_and_timeout_retryable`, `test_key_never_logged` (captured logs), `test_openai_backend_needs_no_models` (`--check` skips the model check).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: add the external OpenAI-compatible transcription backend`.

### Task 11: Image, compose, Helm and CI

**Files:**
- Modify: `Dockerfile` (target `asr`: venv with extra `asr`, neither `ffmpeg` nor `rtmpdump` on `PATH` (the ASR self-check fails when either is there; Task 9 fix round 1), non-root, tini, `CMD text-extraction-asr-worker`, scratch dir), `docker-compose.yml` (services `asr-models`, `asr-worker` under profile `asr`, volume `asr-models`, `ASR_ENABLED` from env), `deploy/helm/text-extraction/values.yaml` and templates (`asr-deployment.yaml`, `asr-pvc.yaml`, ScaledObject/HPA, internal service, `prometheusrule.yaml`, `networkpolicy.yaml` egress for the external endpoint, `ASR_ENABLED` and `MEDIA_*` in every pod, scratch emptyDir with `sizeLimit`), chart `README.md`, `deploy/helm/test.sh`; `.github/workflows/ci.yml` (job `media`: `uv sync --extra asr`, model cache by manifest hash, `--download-models` of `small` for speed plus the configured default on a weekly schedule, `pytest -m asr`; helm lint with ASR on; licence gate over the `asr` extra), `.github/workflows/image.yml` (variant `asr`: build, `--check`, "API image has no faster_whisper, ctranslate2, av, deno", size printed)
- Test: `deploy/helm/test.sh` cases

**Interfaces:**
- Produces: Helm values `workers.asr.{enabled: false, replicaCount: 1, concurrency: 1, backend: local, model: large-v3-turbo, image, resources (requests 2 CPU/3Gi, limits 4 CPU/6Gi; placeholders until package 11), scratch.sizeLimit: 2Gi, terminationGracePeriodSeconds: 60, autoscaling.keda|hpa}`, `workers.asr.uploadMaxBytes: 25000000`, `workers.asr.models.{download: true, downloadMode: init, persistence: {size: 4Gi, accessModes: [ReadWriteMany], storageClass: "", existingClaim: ""}}`, `workers.asr.external.{baseUrl, model, existingSecret}`.

- [ ] **Step 1: Failing tests:** helm cases `asr off renders nothing`, `downloadMode job renders a hook Job and no init container (asr and docling)`, `scratch sizeLimit smaller than MEDIA_MAX_BYTES fails the render`, `asr on renders deployment, PVC, init container writable and worker read-only, ASR_ENABLED=true in every pod`, `external backend renders no PVC and no init container and the key from the secret`, `existingClaim skips the PVC`; the licence gate passes over the `asr` extra (LGPL warnings only).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `bash deploy/helm/test.sh`, the helm lint matrix, `docker compose --profile asr config` pass; the image builds and `--check` run in GitHub Actions.
- [ ] **Step 5:** commit `feat: ship the asr worker image, compose profile and Helm toggle`.

### Task 12: Done-when, live run and docs

**Files:**
- Create: `tests/media/test_corpus.py` (default suite: captions fixtures end to end through the pipeline with a fake media fetcher; marker `asr`: the upload of `speech_de.ogg` through `POST /v1/extract/file` → 202 → job result with word recall ≥ 0.8)
- Modify: `tests/live/run_live.py` and `tests/live/README.md` (set `media` with `transcribe` per row; summary by platform and `transcript_source`; report, not gated), `.github/workflows/live.yml` (media set reported), `docs/architecture.md` (new section "Audio and video" with M1-M16 and D1-D4 as decided; Overview, pipeline step 5, feature note, "Known limits"), `docs/packages.md` (note D1's choice), `README.md` (examples: YouTube URL, `transcribe: true`, audio upload), `CONTRIBUTING.md` (M16)

- [ ] **Step 1:** corpus test → PASS in the default suite; `-m asr` → PASS in CI.
- [ ] **Step 2:** live run on Jan's PC through Remote Control in `C:\Users\jan\te-pkg6-verify` with `docker compose --profile asr up`: the media set, an audio upload, image sizes and the ASR child's peak memory. Done when every YouTube row with subtitles and every Mediathek row with subtitles answers `manual_subtitles` or `auto_subtitles`, and the upload is transcribed.
- [ ] **Step 3:** label the PR `live`: `wlo_200` auto must stay ≥ 80 %; the media set's CI result is reported (data-centre IPs may be refused by YouTube, D1).
- [ ] **Step 4:** docs; commit `test: add the media corpus and live media set, document the package`.

### Final: review and hand-over

- [ ] `requesting-code-review` over the whole branch (Review Focus above as the brief), fixes via `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run pytest -m asr` (CI job `media`), `uv run pytest -m browser` (CI), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, helm lint matrix, image workflow green, live results reported; done-when met.
- [ ] `finishing-a-development-branch`: PR, squash merge once green (Jan's standing permission), progress memory updated.
