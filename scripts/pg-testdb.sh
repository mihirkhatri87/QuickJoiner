#!/usr/bin/env bash
# Bring up a local pgvector database for the env-gated Postgres suite, and print its DSN.
#
# Why this exists as its own script rather than living inside the session hook: the cloud
# sandbox can reap a long-running server during an idle gap (observed 2026-08-15 — the
# cluster went down between two tool calls with no shutdown entry in its own log), and
# `tests/test_pg_backend.py` then ERRORS on connection-refused rather than skipping, which
# reads like a code failure. Re-running this fixes it in one command.
#
# Idempotent: safe to run repeatedly, whether the cluster is up, down, or the database
# already exists. Docker is NOT required — Postgres ships in the image and pgvector is one
# apt package, which is what opens a test gate this repo has twice found defects hiding
# behind (see CLAUDE.md's note on env-gated suites rotting).
set -euo pipefail

PG_VER="${PG_VER:-16}"
DB="${QJ_TEST_DB:-qjtest}"
PW="${QJ_TEST_DB_PASSWORD:-qj}"

if ! command -v pg_ctlcluster >/dev/null 2>&1; then
  echo "pg-testdb: PostgreSQL is not installed; skipping (the pg suite will skip too)" >&2
  exit 1
fi

# pgvector: the one piece not already in the image.
if ! dpkg -s "postgresql-${PG_VER}-pgvector" >/dev/null 2>&1; then
  echo "pg-testdb: installing postgresql-${PG_VER}-pgvector…" >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q "postgresql-${PG_VER}-pgvector" >/dev/null
fi

if ! pg_isready -q 2>/dev/null; then
  # A previous unclean death leaves a stale pid file; start handles that itself.
  pg_ctlcluster "$PG_VER" main start >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do pg_isready -q 2>/dev/null && break; sleep 0.5; done
fi
if ! pg_isready -q 2>/dev/null; then
  echo "pg-testdb: cluster did not come up; see /var/log/postgresql/" >&2
  exit 1
fi

# Password auth over TCP is what the DSN uses; the socket-auth default is not enough.
su postgres -c "psql -tAc \"ALTER USER postgres PASSWORD '${PW}'\"" >/dev/null
if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB}'\"" | grep -q 1; then
  su postgres -c "createdb '${DB}'" >/dev/null
fi
su postgres -c "psql -d '${DB}' -tAc 'SET client_min_messages=warning; CREATE EXTENSION IF NOT EXISTS vector'" >/dev/null

echo "postgresql://postgres:${PW}@localhost:5432/${DB}"
