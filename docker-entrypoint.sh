#!/bin/sh
# Initialize the workspace on first boot only (so UI-changed settings survive
# restarts), then start the server. All state lives under the /data volume.
set -e

: "${QJ_WORKSPACE:=/data/workspace}"
mkdir -p "$QJ_WORKSPACE" "${FASTEMBED_CACHE_PATH:-/data/.cache/fastembed}"

# On-prem state is the SQLite file; cloud state lives in Postgres (DATABASE_URL).
# qj init is idempotent (it only sets the provider on first init), so running it
# every cloud boot is safe and applies QJ_PROVIDER once.
if [ -n "$DATABASE_URL" ] || [ ! -f "$QJ_WORKSPACE/catalog.db" ]; then
  qj init "${QJ_ORG:-default}" --provider "${QJ_PROVIDER:-anthropic}" --workspace "$QJ_WORKSPACE"
fi

exec qj serve --host 0.0.0.0 --port "${QJ_PORT:-8787}" --workspace "$QJ_WORKSPACE"
