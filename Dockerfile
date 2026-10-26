# syntax=docker/dockerfile:1

# Four images from one file:
#   default target (`runtime`, the last stage): the API and extract worker image, no browser, no
#   Docling, no torch, no Tesseract, no Whisper;
#   target `browser`: the browser worker (`text-extraction-browser-worker`), with Chromium,
#   Google Chrome and Xvfb (docker build --target browser);
#   target `docling`: the Docling worker (`text-extraction-docling-worker`), with Docling, CPU torch
#   and the Tesseract CLI, but no models: they come from a volume at /models;
#   target `asr`: the ASR worker (`text-extraction-asr-worker`), with faster-whisper, PyAV built
#   against an LGPL FFmpeg (decision D5) and yt-dlp with Deno, but no models (a volume at /models)
#   and no `ffmpeg` or `rtmpdump` program.
# Every image runs tini as PID 1 (ENTRYPOINT): it forwards signals to the service and reaps
# orphaned child processes, such as a tesseract whose conversion child was killed. Kubernetes and
# compose pass the entry point as args (CMD), so tini stays.

# tini 0.19.0 (MIT, github.com/krallin/tini), the release binary for the build's architecture,
# checked against its SHA-256 (the release's .sha256sum files).
FROM python:3.13-slim AS tini
ARG TARGETARCH
ADD https://github.com/krallin/tini/releases/download/v0.19.0/tini-${TARGETARCH} /tini
RUN case "$TARGETARCH" in \
        amd64) sum=93dcc18adc78c65a028a84799ecf8ad40c936fdfc5f2a57b1acda5a8117fa82c ;; \
        arm64) sum=07952557df20bfd2a95f9bef198b445e006171969499a1d361bd9e6f8e5e0e81 ;; \
        *) echo "no tini checksum for $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && echo "$sum  /tini" | sha256sum -c - \
    && chmod 0755 /tini

# Build stage: resolve the locked dependencies and install the project into /app/.venv.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Build stage of the browser worker: the same environment plus the extra `browser` (Playwright,
# Patchright, autoconsent). Only the browser image copies it.
FROM build AS build-browser
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra browser

# Build stage of the Docling worker: the same environment plus the extra `docling` (Docling, torch
# and torchvision from the PyTorch CPU index). Only the Docling image copies it.
FROM build AS build-docling
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra docling

# Build stage of the ASR worker: the same environment plus the extra `asr`, with PyAV built from
# source against a pinned LGPL FFmpeg (decision D5). The PyPI wheel of av bundles an FFmpeg that
# links the GPL libx264 and libx265, which the worker's self-check refuses; this build has neither.
#
# FFmpeg: the "lgpl-shared" build of BtbN/FFmpeg-Builds (github.com/BtbN/FFmpeg-Builds), release
# autobuild-2026-09-30-13-08 (a month-end build, which the project keeps for long), FFmpeg n9.0.2
# (PyAV 19.0.1 supports the 9.x series; its own wheel bundles 9.0.2). Configured with
# --enable-version3 (LGPL-3.0-or-later), without --enable-gpl or --enable-nonfree, with
# --disable-libx264 --disable-libx265 and --enable-libopus (the external backend encodes Ogg Opus).
# Its external libraries are linked statically into the shared FFmpeg libraries, which need only
# glibc. The asset is checked against its SHA-256 from the release's checksums.sha256. Only the
# shared libraries (lib/) and the licence reach the image: neither bin/ffmpeg, ffprobe nor ffplay.
# To move to a newer build: pick a month-end autobuild release, take the linux64 and linuxarm64
# "lgpl-shared-9.x" names and lines from its checksums.sha256, and change FFMPEG_RELEASE,
# FFMPEG_VERSION, FFMPEG_SERIES and both sums together.
FROM build AS build-asr
ARG TARGETARCH
ARG FFMPEG_RELEASE=autobuild-2026-09-30-13-08
ARG FFMPEG_VERSION=n9.0.2-17-g2a571b6068
ARG FFMPEG_SERIES=9.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev pkg-config xz-utils \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
        amd64) platform=linux64; sum=7a1ba2af36e5beeb98151e766a903b1d115f2206a86571f17f873cd7292c424f ;; \
        arm64) platform=linuxarm64; sum=1aa02c59afe06dea2602b40517a5cb75c0ff6d524f217b5aea1f0ca07977cd51 ;; \
        *) echo "no FFmpeg build for $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && asset="ffmpeg-${FFMPEG_VERSION}-${platform}-lgpl-shared-${FFMPEG_SERIES}.tar.xz" \
    && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${asset}" \
        /tmp/ffmpeg.tar.xz \
    && echo "$sum  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 \
    && rm -rf /tmp/ffmpeg.tar.xz /opt/ffmpeg/bin /opt/ffmpeg/doc /opt/ffmpeg/man /opt/ffmpeg/presets \
    && echo /opt/ffmpeg/lib > /etc/ld.so.conf.d/ffmpeg.conf \
    && ldconfig
# The build tools of PyAV's sdist, pinned (14-day rule) with the SHA-256 of their PyPI wheels
# (cython: cp313 manylinux x86_64 and aarch64, and the pure-Python fallback); `uv build
# --require-hashes` refuses anything else. Its build-system asks for setuptools>=78 and
# cython>=3.3,<4. Moving either: take the sums from https://pypi.org/pypi/<name>/<version>/json.
RUN mkdir -p /build && printf '%s\n' \
    'cython==3.3.0 --hash=sha256:23942b0662642927a55676e4b26e6840fb166dd7d76436384685227e7e8619a4 --hash=sha256:bc2f2a6b65a991666cfd35a35bab0cd88ffba4df2f601edb6e76cc8116de24b9 --hash=sha256:9b24b5c8cd536946b62086fcafee6d5509d3f549f72d553d2336af87ffbe0da1' \
    'setuptools==84.0.0 --hash=sha256:51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670' \
    > /build/av-build-constraints.txt
# PyAV's sdist exactly as uv.lock pins it (URL and SHA-256), built into a wheel against the FFmpeg
# above (pkg-config finds it; its .pc files go afterwards), then the extra `asr` without av's PyPI
# wheel, then the built wheel.
RUN --mount=type=cache,target=/root/.cache/uv <<BUILD
set -eu
python - <<'SDIST'
import hashlib, tomllib, urllib.request
with open("/app/uv.lock", "rb") as file:
    lock = tomllib.load(file)
avs = [package for package in lock["package"] if package["name"] == "av"]
if len(avs) != 1:
    raise SystemExit(f"uv.lock holds {len(avs)} av packages, this build handles exactly one")
(av,) = avs
sdist = av["sdist"]
with urllib.request.urlopen(sdist["url"]) as response:
    data = response.read()
if "sha256:" + hashlib.sha256(data).hexdigest() != sdist["hash"]:
    raise SystemExit("the av sdist does not match uv.lock")
with open(f"/build/av-{av['version']}.tar.gz", "wb") as file:
    file.write(data)
SDIST
PKG_CONFIG_PATH=/opt/ffmpeg/lib/pkgconfig uv build --wheel --python /usr/local/bin/python3 \
    --build-constraints /build/av-build-constraints.txt --require-hashes \
    --out-dir /build/wheels /build/av-*.tar.gz
rm -rf /opt/ffmpeg/lib/pkgconfig
uv sync --locked --no-dev --no-editable --extra asr --no-install-package av
uv pip install --python /app/.venv/bin/python --no-deps /build/wheels/av-*.whl
BUILD

# Browser worker image. It serves only the internal port 9090. Playwright's headless Chromium
# shell renders standard pages; Google Chrome stable (Patchright, channel `chrome`) renders
# unblock mode, headful under Xvfb, which the worker starts itself. Chrome stable exists for
# linux/amd64 only, so this image is amd64 only.
FROM python:3.13-slim AS browser
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=tini /tini /usr/bin/tini
COPY --from=build-browser /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# As root: the system libraries of both browsers, Chromium's headless shell into
# /opt/ms-playwright and Chrome stable into /opt/google/chrome (from Google's .deb). Chrome is
# not pinned: every build installs the Chrome stable of that day. The headless shell is the one
# the locked Playwright names.
# Patchright's installer adds curl (only to download the .deb; nothing at runtime uses it) and
# Chrome's .deb adds Google's apt source, its signing key and a cron job that re-adds the source:
# all removed again, so the image has no extra apt source. curl goes only when nothing depends on it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && playwright install --with-deps --only-shell chromium \
    && patchright install --with-deps chrome \
    && rm -f /etc/apt/sources.list.d/google-chrome.list /etc/apt/sources.list.d/google-chrome.sources \
        /etc/apt/trusted.gpg.d/google-chrome.gpg /usr/share/keyrings/google-chrome.gpg \
        /etc/cron.daily/google-chrome \
    && if [ "$(dpkg-query -W -f='${db:Status-Status}' curl 2>/dev/null)" = installed ]; then \
        apt-mark auto curl && apt-get autoremove -y --purge; fi \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
# The root filesystem is read-only in compose and Kubernetes, and the user has no home: Chrome's
# profile helpers, crash reporter and font cache write under HOME and the XDG directories, so
# they point into the writable /tmp (a tmpfs or emptyDir).
ENV HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache
EXPOSE 9090
USER 10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["text-extraction-browser-worker"]

# Docling worker image. It serves only the internal port 9090 and holds no models: Docling's
# layout and table models and Tesseract's data are on the volume at /models (DOCLING_MODELS_PATH;
# `text-extraction-docling-worker --download-models` fills it). Tesseract comes from Debian
# (Apache-2.0) without language packs beyond the two its package depends on (eng, osd);
# TESSDATA_PREFIX points at the volume's tessdata_best files instead.
FROM python:3.13-slim AS docling
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
# /models stays empty: it is the mount point. Owned by the user, so a new docker volume mounted
# there is writable for the download (Kubernetes uses fsGroup instead).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache \
    && install -d -o 10001 -g 10001 /models
COPY --from=tini /tini /usr/bin/tini
COPY --from=build-docling /app/.venv /app/.venv
# The root filesystem is read-only in compose and Kubernetes, and the user has no home: torch and
# Hugging Face write their caches under HOME, so it points into the writable /tmp.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCLING_MODELS_PATH=/models \
    TESSDATA_PREFIX=/models/tessdata \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache
EXPOSE 9090
USER 10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["text-extraction-docling-worker"]

# ASR worker image. It serves only the internal port 9090 and holds no models: the Whisper model is
# on the volume at /models (ASR_MODELS_PATH; `text-extraction-asr-worker --download-models` fills
# it). FFmpeg is there as shared libraries only (/opt/ffmpeg/lib, registered with ldconfig, so the
# worker's children find them: their allowlisted environment has no LD_LIBRARY_PATH), never as a
# program: neither `ffmpeg` nor `rtmpdump` is on PATH (yt-dlp would hand them the protocols it
# does not download itself; the worker's self-check refuses either). Deno, yt-dlp's JavaScript
# runtime for YouTube's player challenges, is the binary of the `deno` package in the venv: yt-dlp
# gets its path from deno.find_deno_bin() (media/asr_child.py), not from PATH. Downloaded media go
# to /scratch (ASR_SCRATCH_PATH; an emptyDir with a size limit in Kubernetes, a volume in compose).
FROM python:3.13-slim AS asr
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build-asr /opt/ffmpeg/lib /opt/ffmpeg/lib
COPY --from=build-asr /opt/ffmpeg/LICENSE.txt /opt/ffmpeg/LICENSE.txt
# /models and /scratch stay empty: they are mount points, owned by the user, so a new docker volume
# mounted there is writable (Kubernetes uses fsGroup instead).
RUN echo /opt/ffmpeg/lib > /etc/ld.so.conf.d/ffmpeg.conf \
    && ldconfig \
    && install -d -o 10001 -g 10001 /models /scratch
COPY --from=tini /tini /usr/bin/tini
COPY --from=build-asr /app/.venv /app/.venv
# The root filesystem is read-only in compose and Kubernetes, and the user has no home: Hugging
# Face and Deno keep their caches under /tmp (the worker points HF_HOME and DENO_DIR there).
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASR_MODELS_PATH=/models \
    ASR_SCRATCH_PATH=/scratch \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache
# The build fails unless PyAV runs on an LGPL FFmpeg (the worker's own licence check) that has the
# libopus encoder, and neither ffmpeg, ffprobe nor rtmpdump is on PATH.
RUN <<CHECK
set -eu
python - <<'PY'
import shutil
import av
import av.codec
from text_extraction.asr_worker import ffmpeg_gpl_findings
findings = ffmpeg_gpl_findings(av._core.library_meta, av.codecs_available)
if findings:
    raise SystemExit(f"PyAV's FFmpeg is not an LGPL build: {findings}")
av.codec.Codec("libopus", "w")
tools = [tool for tool in ("ffmpeg", "ffprobe", "rtmpdump") if shutil.which(tool)]
if tools:
    raise SystemExit(f"programs on PATH: {tools}")
print("FFmpeg", av.ffmpeg_version_info, "is LGPL and has the libopus encoder")
PY
CHECK

EXPOSE 9090
USER 10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["text-extraction-asr-worker"]

# Runtime stage (default target): only the virtual environment, no build tools, models, data or
# browsers.
FROM python:3.13-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=tini /tini /usr/bin/tini
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080 9090
USER 10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["text-extraction"]
