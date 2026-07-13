# QuickJoiner — web UI + API + scheduler in one container.
#
# Lean by default (no headless browser). To include the Playwright browser used
# by the web_scrape fallback, build with:  docker build --build-arg WITH_BROWSER=1 .

# --- stage 1: build the React web UI -------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

# --- stage 2: the app ------------------------------------------------------
FROM python:3.12-slim

ARG WITH_BROWSER=0
# Comma-separated optional extras to install, e.g. EXTRAS=cloud for Postgres/pgvector.
ARG EXTRAS=""
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    QJ_WORKSPACE=/data/workspace \
    QJ_UI_DIR=/app/ui \
    FASTEMBED_CACHE_PATH=/data/.cache/fastembed \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# git: the `git` connector clones repos. curl: container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY quickjoiner ./quickjoiner
COPY --from=ui /ui/dist ./ui

RUN if [ -n "$EXTRAS" ]; then pip install ".[$EXTRAS]"; else pip install .; fi \
 && if [ "$WITH_BROWSER" = "1" ]; then \
        pip install ".[browser]" \
     && mkdir -p /ms-playwright \
     && python -m playwright install --with-deps chromium \
     && chmod -R a+rX /ms-playwright ; \
    fi

# Run as a non-root user; /data is the persistent volume it owns.
RUN useradd -m qj && mkdir -p /data && chown -R qj:qj /data
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER qj
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=45s --retries=3 \
  CMD curl -fsS http://localhost:8787/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
