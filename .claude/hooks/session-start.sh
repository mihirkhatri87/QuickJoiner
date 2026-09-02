#!/bin/bash
# Reconstruct the dev environment for a Claude Code on the web session.
#
# The container is ephemeral — it is reclaimed on inactivity or session end and the repo is
# re-cloned fresh — so nothing installed in one session survives to the next. This makes the
# environment reproducible instead, which matters most for the Postgres suite: it is env-gated
# on QJ_TEST_DATABASE_URL, and CLAUDE.md records two defects that accumulated behind that gate
# while it was quietly being skipped. With this hook the gate is open by default.
set -euo pipefail

# Local machines already have a working venv (see CLAUDE.md's dev-environment note, which
# forbids the system Python there); this only ever sets up the disposable web container.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# The base image carries a Debian-packaged cryptography with no RECORD file, which pip cannot
# uninstall and so refuses to upgrade over. It is dealt with in its OWN command, because
# `--ignore-installed` is a GLOBAL flag, not a per-package one: appending it to the main
# install means "ignore everything already installed", which reinstalls the entire dependency
# tree at latest versions. That is not theoretical — an earlier draft of this hook did exactly
# that and pulled LanceDB 0.38, where a double-quoted string in a filter predicate is parsed as
# an identifier, taking 58 tests down with it. The image ships a curated, working set; the job
# here is to ADD to it, never to re-resolve it.
# `browser` is deliberately omitted — no test needs Playwright, and it costs a Chromium fetch.
echo "session-start: installing Python dependencies…"
pip3 install -q --ignore-installed cryptography --root-user-action=ignore
pip3 install -q -e ".[dev,cloud]" --root-user-action=ignore

# The frontend typecheck is a real gate here: this repo's house rule is that every API change
# is a UI change until proven otherwise, and `npm run build` (tsc + vite) is how that is
# checked. Installing costs ~8s in this container, so it is not worth deferring; the build
# itself stays on demand (~20s here, minutes on a slower machine).
if [ -f frontend/package.json ] && command -v npm >/dev/null 2>&1; then
  echo "session-start: installing frontend dependencies…"
  (cd frontend && npm install --no-audit --no-fund -s) || \
    echo "session-start: frontend install failed — 'npm run build' will not work." >&2
fi

# Best-effort: a missing database must leave the pg tests SKIPPED (their normal gated state),
# never fail the hook and block the session over a suite that is optional by design.
echo "session-start: preparing the pgvector test database…"
if DSN="$(./scripts/pg-testdb.sh 2>/dev/null)"; then
  echo "export QJ_TEST_DATABASE_URL=\"${DSN}\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"
  echo "session-start: Postgres suite enabled (${DSN})"
else
  echo "session-start: no pgvector database — the Postgres suite will skip." >&2
  echo "session-start: re-run ./scripts/pg-testdb.sh to enable it." >&2
fi

echo "session-start: ready — python3 -m pytest -q"
