# Package 6 spike results (Task 1)

Run on 2026-10-25 (about 19:20-20:45 UTC) on Jan's PC through Remote Control, in a fresh clone (`C:\Users\jan\te-pkg6-verify`) with a scratch venv. Nothing in the staging folder was used.

- Machine: AMD Ryzen 7 7730U (8 cores / 16 threads, 15 W laptop CPU), 15.3 GB RAM, Windows 11, a German residential line.
- Python 3.13.5. Pins: `yt-dlp[default]==2026.8.19` (with `yt-dlp-ejs==0.8.0`), `youtube-transcript-api==1.2.4`, `faster-whisper==1.2.1`, `ctranslate2==4.8.2`, `av==19.0.1`, `deno==2.9.7`. A second venv with `yt-dlp==2026.10.21` was used where a row failed.
- User agent for page requests in both libraries: `text-extraction/0.1.0 (+https://github.com/janschachtschabel/text-extraction)` (the `USER_AGENT` of `fetch.py`).
- yt-dlp options as M4: `allowed_extractors` (one extractor), `noplaylist`, `skip_download`, `cachedir: False`, no cookies, `extractor_retries: 0`, `socket_timeout: 10`. "Without Deno" is `js_runtimes: {}`; "with Deno" is `js_runtimes: {"deno": {"path": <the deno wheel's binary>}}`.

## Summary

- **YouTube works from Jan's line, with or without Deno.** Tier 1 found subtitles for 8 of 9 YouTube rows, with youtube-transcript-api and with yt-dlp, without Deno as well as with it (the 9th has subtitles disabled: `TranscriptsDisabled`, correctly `metadata_only`). No bot check, no `PoTokenRequired`, no 429. Even without Deno, yt-dlp listed audio-only formats and a 1 MB range request on the best one answered 206 for every row. Deno changed nothing here. (One Deno run hit a 10 s read timeout once; the rerun passed.)
- **Mediatheken work without Deno.** ARD 3 of 4, ZDF 3 of 4, 3sat 1 of 2 and arte 1 of 2 rows have a full German track. Each row without one has no track at the source either, so the expected answer is `metadata_only`.
- **Vimeo blocks `vimeo.com/<id>`.** yt-dlp needs a login for these URLs in both 2026.8.19 and 2026.10.21. The player URL `player.vimeo.com/video/<id>` works without login and without impersonation. The two WLO Vimeo videos are embed-restricted (the player answers 403), so they can only be `metadata_only`. A newer yt-dlp does not help, so the M16 exception does not apply.
- **Whisper speed:**
  - `large-v3-turbo` runs at RTF 0.88 on 5 minutes (1.15 on the 30 s clip, including the start-up of the first window). Peak RSS is 1.9 GB.
  - `small` runs at RTF 0.43 and peaks at 0.7 GB.
  - Both recall 57 of 58 words on the clean clip.
- **Blocker 1, licence:** the PyAV 19.0.1 wheels bundle libx264 and libx265 (GPL-2.0-or-later), and libavcodec links them dynamically. The FFmpeg configuration has no `--enable-gpl` and reports "LGPL version 3 or later", so the check the plan asks for passes. The wheel still ships GPL libraries. This needs a decision (section "PyAV").
- **Blocker 2, compatibility (small):** faster-whisper 1.2.1's own `decode_audio` calls `av.open(..., metadata_errors="ignore")`, and PyAV 19 removed that argument (TypeError). Passing a file path to `transcribe()` fails. Our own PyAV decode to a float32 array works, and M10 already decodes that way. So: never pass a path, and add a test.

## Step 1: media sample set (`tests/live/urls/media.csv`)

Columns of the per-URL table:
- **YTA:** youtube-transcript-api, `list` and then `fetch` of the chosen track.
- **yt-dlp:** `extract_info(download=False)` without Deno. The chosen track was downloaded; the table gives its format and bytes.
- **+Deno:** the same with Deno, YouTube rows only.

Cells read: found / source / language / format / size / seconds. Times include the track download.

### YouTube

| Row | YTA | yt-dlp (no Deno) | +Deno | Audio-only formats, 1 MB range | Duration, chapters, licence |
| --- | --- | --- | --- | --- | --- |
| MrWissen2go `uN1Zx-Ia-SE` | yes / manual / de / 374 cues / 1.7 s | yes / manual / `de-XwLwiJMB_Xs` / vtt / 38,806 B / 2.8 s | same / 2.7 s | 10, 206 | 1118 s, 0, none |
| Terra X History `5At8hsfPHqU` | yes / manual / de / 102 cues / 2.1 s | yes / manual / de / vtt / 16,500 B / 2.7 s | same / 2.6 s | 10, 206 | 995 s, 6, none |
| MAITHINK X `BFmsurpE-Ho` | yes / manual / de / 171 cues / 1.4 s | yes / manual / de / vtt / 13,960 B / 2.7 s | same / 2.7 s | 3, 206 | 520 s, 0, none |
| Kurzgesagt DE `8rLrQFRkrgE` | yes / manual / de / 195 cues / 1.9 s | yes / manual / de / vtt / 27,700 B / 2.7 s | same / 2.7 s | 10, 206 | 1318 s, 0, none |
| MrWissen2go Geschichte `T8fgOmSrigo` | yes / manual / de / 163 cues / 1.6 s | yes / manual / de / vtt / 23,769 B / 2.7 s | same / 2.8 s | 10, 206 | 853 s, 5, none |
| Kurzgesagt DE `HPjZDlQexeY` (auto only) | yes / auto / de / 431 cues / 1.6 s | yes / auto / `de-orig` / vtt / 127,421 B / 2.3 s | same / 2.5 s | 10, 206 | 1018 s, 0, none |
| MrWissen2go Geschichte `1b3SJ2z1b2I` (auto only) | yes / auto / de / 403 cues / 11.7 s | yes / auto / `de-orig` / vtt / 127,543 B / 8.7 s | same / 2.6 s | 10, 206 | 877 s, 6, none |
| WLO `6g6X353YgKk` | no / `TranscriptsDisabled` / 1.5 s | no track / 2.5 s | no track / 2.2 s | 3, 206 | 23 s, 0, CC BY |
| WLO `XAgqWLNlseg` | yes / auto / de / 133 cues / 1.5 s | yes / auto / `de-orig` / vtt / 39,544 B / 3.0 s | same / 3.0 s | 3, 206 | 335 s, 0, CC BY |

- yt-dlp offers each YouTube track in `json3, srt, srv1, srv2, srv3, ttml, vtt`. The automatic captions list 157 languages, which are `de-orig`, `de` and machine translations.
- The "CC BY" licence string is literally `Creative Commons Attribution license (reuse allowed)`.
- `language` (original language) is `de` except for the 23 s WLO short (`null`).
- Without Deno, yt-dlp warns once per call: "No supported JavaScript runtime could be found".

### Vimeo

| Row | yt-dlp 2026.8.19 | yt-dlp 2026.10.21 | Notes |
| --- | --- | --- | --- |
| WLO `vimeo.com/47677979` | no / `DownloadError`: "The web client only works when logged-in" / 1.2 s | same | page 200, player 403, oEmbed 404: embed-restricted |
| WLO `vimeo.com/70845432` | same / 1.3 s | same | same |
| `player.vimeo.com/video/76979871` (public) | yes / manual / de / vtt (HLS, 14 cues, 1,304 B) / 3.0 s | yes | `vimeo.com/76979871` fails like the WLO rows |

- With `curl_cffi` installed (impersonation), `vimeo.com/<id>` still needs a login. The player URL works without it, with only a warning. Impersonation would also break the honest user agent (rule 6), so the ruling should not use it.

### Mediatheken

| Row | yt-dlp (no Deno) | Duration, chapters |
| --- | --- | --- |
| ARD extra 3 (NDR) | yes / manual / `deu` / vtt / 82,267 B / 1.2 s | 2612 s, 8 |
| ARD Einspruch, Schatz! | yes / manual / `deu` / vtt / 152,264 B / 1.2 s | 5291 s, 0 |
| ARD Tatort (BR) | yes / manual / `deu` / vtt / 136,093 B / 1.1 s | 5246 s, 0 |
| ARD hr Doku "Chip im Kopf" | no track (no UT at the source) / 1.2 s | 2641 s, 0 |
| ZDF Terra X (new `/video/` URL) | yes / manual / `deu` / vtt / 49,397 B / 2.7 s | 2625 s, 0 |
| ZDF ZDFzeit | yes / manual / `deu` / vtt / 68,981 B / 1.6 s | 2624 s, 0 |
| ZDF ZDFinfo | yes / manual / `deu` / vtt / 44,724 B / 1.4 s | 2667 s, 0 |
| ZDF trailer (old `/dokumentation/...html` URL) | no track / 1.3 s | 55 s, 0 |
| arte "Die Mütter von Cherson" | yes / manual / de / vtt via HLS: 118,988 B, 1,306 cues / 1.3 s | 9413 s, 0 |
| arte "Byzanz" | only `de-forced` (8 cues, 1,584 B) and `en` / 1.1 s | 5391 s, 0 |
| 3sat Theatertreffen "Il Gattopardo" | yes / manual / `deu` / vtt / 222,123 B / 2.3 s | 10706 s, 0 |
| 3sat nano Doku | no track / 1.9 s | 2636 s, 0 |

- ARD and ZDF offer `vtt` plus `ttml` (ARD) or `xml` (ZDF). No Mediathek gives a `license` field.
- How common subtitles were among the additional candidates I checked:
  - ARD: 5 of 7 (two feature films had none).
  - 3sat: 1 of 7 (UT is rare there).
  - arte: 1 of 9 had a full `de` track; 7 had only `de-forced`.

### Direct files and HTML5 tracks

| Row | Result |
| --- | --- |
| BR radioWissen MP3 | Range GET 206, `audio/mpeg`, sniffs `ID3`, 21.7 MB, 1.5 s |
| tagesschau in 100 Sekunden MP4 | Range GET 206, `video/mp4`, sniffs `ftyp`, 32.1 MB, 0.8 s (a daily file; replace it when it is gone) |
| Commons "Die Schichten der Erdatmosphäre" | page with 2 media elements and 2 `<track>` (de), track `text/vtt`, 4,513 B, 1.7 s |
| Commons "Tropfsteine als Indikatoren ..." | same, track 1,413 B, 1.7 s |

### Findings for the rulings

1. **M1, `allowed_extractors` names.** The option matches yt-dlp's `IE_NAME`, as a case-insensitive full regex match, not the class key. An unknown name loads nothing silently ("No suitable extractor"), which is how ARD and 3sat first failed. The names are `youtube`, `vimeo`, `ardmediathek` (class `ARDBetaMediathek`), `zdf`, `artetv` and `3sat` (class `DreiSat`). They should be pinned in `media/sites.py`, with a unit test that each one matches exactly one extractor in the pinned version. The ZDF extractor takes the new `zdf.de/video/<section>/<show>/<episode>` URLs as well as the old `.../*.html` ones.
2. **M1, Vimeo.** The plan should normalise `vimeo.com/<digits>` to `https://player.vimeo.com/video/<digits>` for yt-dlp, which works without login. When the player answers 403 (embed-restricted, as both WLO rows), the answer is tier 2 from the fast fetch of the `vimeo.com` page (200; oEmbed answered 404).
3. **M2, language matching.**
   - Track keys must be compared by their primary subtag after normalisation:
     - Mediatheken use ISO 639-2 `deu`.
     - YouTube manual tracks can carry a named key (`de-XwLwiJMB_Xs`).
     - YouTube automatic captions come as `de-orig` (the original-language ASR) plus `de`. All other automatic languages are machine translations, so `<lang>-orig` (else the original language) is the only automatic track to take.
   - arte's `-forced` tracks only cover foreign-language passages, so they must never count as a transcript (skip them).
   - arte also lists `und`.
4. **M3/M7, HLS subtitles.** arte and Vimeo give subtitles with `protocol: m3u8_native`: the URL is an HLS playlist whose segment is the VTT file. The child must read the playlist (`#EXTM3U`) and fetch and concatenate its segments, within the byte cap and through the proxy. A plain download returns only the 199-byte playlist.
5. **D1.** From a residential line, YouTube did not refuse once. Tier 1 needs no Deno, and audio download (Tier 3) also worked without Deno today. From a data-centre IP this may differ (not testable here).

## Step 2: Whisper speed

The clips:
- `tests/media/corpus/speech_de.ogg`: 30.0 s, the first two sentences of Kafka's "Die Verwandlung", chapter 1 (LibriVox, public domain). Cut at a gap between words, 16 kHz mono input, libopus at 32 kbit/s in Ogg, made with PyAV.
- The expected text is the Projekt Gutenberg wording (58 words). The reader says "ungeheuren" where the book has "ungeheueren".
- The 5-minute clip (not committed) is 300 s from the same place.
- `src/text_extraction/media/asr_check.ogg` holds the clip's first 5 s, where `small` hears "Als Gregor Samsa eines Morgens".

Settings: `device=cpu`, `compute_type=int8`, `cpu_threads=4`, `vad_filter=True`, `condition_on_previous_text=False`, language auto-detected (de, p ≥ 0.99). Each run is a fresh process. Decoding is our own PyAV code (16 kHz mono float32) and counts in the transcription time.

| Model | Clip | Load | Transcribe | RTF | Peak RSS (peak working set) | Word recall |
| --- | --- | --- | --- | --- | --- | --- |
| small | 30 s | 1.8 s | 12.8 s | 0.43 | 695 MB | 0.983 (57/58) |
| small | 5 min | 2.1 s | 129.0 s | 0.43 | 695 MB | - |
| large-v3-turbo | 30 s | 6.9 s | 34.6 s | 1.15 | 1,880 MB | 0.983 (57/58) |
| large-v3-turbo | 5 min | 6.2 s | 265.2 s | 0.88 | 1,880 MB | - |

- The one word both models miss is "ungeheueren" (the reader's variant).
- `large-v3-turbo` wrote the 30 s clip in lower case without punctuation, but the 5-minute clip in normal case.
- On this clean read speech the two models are equally accurate, so the clip does not separate them on quality.
- **D2:** on this 15 W laptop CPU with 4 threads, `large-v3-turbo` is only just faster than real time on realistic lengths (0.88) and slower on short clips. By D2's rule it stays the default, with a thin margin. One hour of audio takes about 53 minutes of a worker slot; at `MEDIA_MAX_DURATION_SECONDS` (3 h) it takes about 2.6 h, so `ASR_TIMEOUT_SECONDS` and the lease have to cover that. `small` is about 2× faster with a third of the memory. Jan may want to confirm D2 with these numbers. A server core is usually faster than this laptop's.

## Step 3: model pins and PyAV

### Hugging Face repositories (faster-whisper 1.2.1 `utils._MODELS`)

- `small` → `Systran/faster-whisper-small`, commit `536b0662742c02347bc0e980a01041f333bce120` (2023-11-23), MIT. It has no `preprocessor_config.json`; faster-whisper uses its defaults (80 mel bins).
- `large-v3-turbo` (also `turbo`) → `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, commit `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` (2025-11-05), MIT.
- faster-whisper's `allow_patterns` are `config.json`, `preprocessor_config.json`, `model.bin`, `tokenizer.json` and `vocabulary.*`.
- The SHA-256 of every file below matched the Hub's LFS hash. Both models loaded and transcribed from directories with only these files.

### Proposed `src/text_extraction/media/models.toml`

```toml
# The Whisper models of the ASR worker, as `text-extraction-asr-worker --download-models` puts them
# under ASR_MODELS_PATH (ruling M13). Each file is fetched from its `url`, which names a fixed
# commit, and must have exactly `size` bytes and this SHA-256; otherwise nothing is put in place.
# Only the files of the configured ASR_MODEL (the first path segment) are downloaded. A change of
# this file (its SHA-256) makes the next run download again. The licences are in NOTICE.

# faster-whisper `small` (MIT), Systran/faster-whisper-small at commit 536b066 (2023-11-23).
[[files]]
path = "small/config.json"
url = "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/config.json"
sha256 = "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"
size = 2370

[[files]]
path = "small/model.bin"
url = "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/model.bin"
sha256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
size = 483546902

[[files]]
path = "small/tokenizer.json"
url = "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/tokenizer.json"
sha256 = "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"
size = 2203239

[[files]]
path = "small/vocabulary.txt"
url = "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/vocabulary.txt"
sha256 = "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"
size = 459861

# faster-whisper `large-v3-turbo` (MIT), mobiuslabsgmbh/faster-whisper-large-v3-turbo at commit
# 0a363e9 (2025-11-05).
[[files]]
path = "large-v3-turbo/config.json"
url = "https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/config.json"
sha256 = "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e"
size = 2263

[[files]]
path = "large-v3-turbo/model.bin"
url = "https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin"
sha256 = "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
size = 1617884929

[[files]]
path = "large-v3-turbo/preprocessor_config.json"
url = "https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/preprocessor_config.json"
sha256 = "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"
size = 340

[[files]]
path = "large-v3-turbo/tokenizer.json"
url = "https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/tokenizer.json"
sha256 = "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd"
size = 2710337

[[files]]
path = "large-v3-turbo/vocabulary.json"
url = "https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/vocabulary.json"
sha256 = "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"
size = 1068114
```

Sizes: `small` 486.2 MB, `large-v3-turbo` 1,621.7 MB. A 4Gi PVC (M17) holds `large-v3-turbo` plus a staged copy.

### PyAV 19.0.1

- `av.library_versions`: libavutil 61.1.102, libavcodec 63.1.102, libavformat 63.1.102, libavdevice 63.1.102, libavfilter 12.1.102, libswscale 10.1.102, libswresample 7.1.102 (FFmpeg 9.0.2). `av.library_meta[*]["license"]` is "LGPL version 3 or later".
- Configuration (Windows wheel; the manylinux_2_28 x86_64 wheel, checked from its bytes, has the same except platform flags): `--enable-version3 ... --enable-libdav1d --enable-libmp3lame --enable-libopus --enable-libsvtav1 --enable-libvmaf --enable-libvpx --enable-libwebp --enable-zlib --enable-libx265 --enable-libx264 --enable-nvenc --enable-nvdec --enable-amf --enable-libvpl --disable-encoder='avui,dca,mlp,opus,s302m,sonic,sonic_ls,truehd' ...`. It contains **no `--enable-gpl`** and no `--enable-nonfree`.
- Encoders: `libopus` yes (the native `opus` encoder is disabled; `opus` resolves to libopus), `libmp3lame` yes, `aac` yes, `flac` yes, `libvorbis` no. M11's Ogg Opus at 32 kbit/s works: that is how the corpus clips were made.
- **GPL inside the wheel.** `av.libs/` of both wheels contains `libx264` (GPL-2.0-or-later) and `libx265` (GPL-2.0-or-later). In the Linux wheel, libavcodec lists `libx264-d6533a8d.so.165` and `libx265-eebb7db1.so.217` among its linked libraries, so they cannot simply be deleted from the image. The wheel's metadata says only BSD (PyAV's own licence), so a metadata-based licence gate does not see this. Options for Jan:
  - (a) Build PyAV from source in the ASR image (`pip install av --no-binary av`) against an FFmpeg built in a builder stage with only decoders, libopus and libmp3lame (LGPL). This is the cleanest, but adds build time and maintenance.
  - (b) Use a published LGPL FFmpeg build (for example BtbN's `*-lgpl` Linux builds) as a binary in the ASR image, decode with a subprocess instead of PyAV, and drop PyAV.
  - (c) Accept the GPL encoders as unused shared libraries. This contradicts rule 7 (no GPL), so it is not recommended.
- **faster-whisper 1.2.1 with av 19.0.1:** `faster_whisper.audio.decode_audio` calls `av.open(..., metadata_errors="ignore")`, and PyAV 19 raises `TypeError` (the argument is gone). `transcribe(<path>)` therefore fails; `transcribe(<float32 array>)` works. The ASR child must always decode with our own code and needs a test for it. Under option (b) this is moot.

## Files committed

- `tests/live/urls/media.csv`: 28 rows (9 YouTube, 3 Vimeo, 4 ARD, 4 ZDF, 2 arte, 2 3sat, 2 files, 2 HTML5 track pages), with expected sources.
- `tests/media/corpus/speech_de.ogg`, `speech_de.expected.txt`, `SOURCES.md`.
- `src/text_extraction/media/asr_check.ogg` (5 s, 16 KB).
