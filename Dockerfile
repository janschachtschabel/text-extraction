# syntax=docker/dockerfile:1

# Two images from one file:
#   default target (`runtime`, the last stage): the API and extract worker image, no browser;
#   target `browser`: the browser worker (`text-extraction-browser-worker`), with Chromium,
#   Google Chrome and Xvfb (docker build --target browser).

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

# Browser worker image. It serves only the internal port 9090. Playwright's headless Chromium
# shell renders standard pages; Google Chrome stable (Patchright, channel `chrome`) renders
# unblock mode, headful under Xvfb, which the worker starts itself. Chrome stable exists for
# linux/amd64 only, so this image is amd64 only.
FROM python:3.13-slim AS browser
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
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
CMD ["text-extraction-browser-worker"]

# Runtime stage (default target): only the virtual environment, no build tools, models, data or
# browsers.
FROM python:3.13-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080 9090
USER 10001
CMD ["text-extraction"]
