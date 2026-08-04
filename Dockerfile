# syntax=docker/dockerfile:1.7
# QuickJoiner — web UI + API + scheduler in one container.
#
# Lean by default (no headless browser). To include the Playwright browser used
# by the web_scrape fallback and remote sign-in, build with:
#   docker build --build-arg WITH_BROWSER=1 .
#
# LAYER ORDER IS LEAD BY REBUILD COST, NOT BY READING ORDER. The two expensive steps are
# the Python dependency install and the ~250MB Chromium download; both depend ONLY on
# pyproject.toml, so they are installed from it alone and the application source is copied
# in *afterwards*. Copying `quickjoiner/` or the built UI before them (as this file used to)
# meant every one-line Python or frontend edit re-resolved every dependency and re-downloaded
# Chromium — minutes per build instead of seconds.

# --- stage 1: build the React web UI -------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
# Cache mount: node_modules is restored from the shared cache instead of refetched whenever
# the lockfile does change. The cache lives outside the image, so it costs no image size.
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

# --- stage 2: the app ------------------------------------------------------
FROM python:3.12-slim

ARG WITH_BROWSER=0
# Comma-separated optional extras to install, e.g. EXTRAS=cloud for Postgres/pgvector.
ARG EXTRAS=""
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QJ_WORKSPACE=/data/workspace \
    QJ_UI_DIR=/app/ui \
    FASTEMBED_CACHE_PATH=/data/.cache/fastembed \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# git: the `git` connector clones repos. curl: container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user; /data is the persistent volume it owns. Early, because it never
# changes — keeping it above the source copies means an edit can't invalidate it.
RUN useradd -m qj && mkdir -p /data && chown -R qj:qj /data

# --- dependencies (expensive, and independent of the application source) ---
# Only the metadata is copied here. A stub package satisfies hatchling so the dependency set
# resolves and installs without the real source, keeping this layer valid across code edits;
# the actual package is installed further down. The dependency list therefore still lives in
# exactly one place (pyproject.toml) — nothing is duplicated into this file to achieve it.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p quickjoiner && touch quickjoiner/__init__.py \
 && if [ -n "$EXTRAS" ]; then pip install ".[$EXTRAS]"; else pip install .; fi \
 && if [ "$WITH_BROWSER" = "1" ]; then pip install ".[browser]"; fi

# Chromium in its own layer: ~250MB of download that must not be redone because an unrelated
# extra changed. Skipped entirely for a lean build.
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        mkdir -p /ms-playwright \
     && python -m playwright install --with-deps chromium \
     && chmod -R a+rX /ms-playwright ; \
    fi

# --- application (cheap, changes constantly) -------------------------------
COPY quickjoiner ./quickjoiner
# --no-deps: everything is already installed above, so this only (re)installs the package
# itself — a couple of seconds, not a full dependency resolve.
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps --force-reinstall .

# After the Python layers, so a frontend-only change can never invalidate them.
COPY --from=ui /ui/dist ./ui

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER qj
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=45s --retries=3 \
  CMD curl -fsS http://localhost:8787/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
