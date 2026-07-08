"""SQLite catalog: sources, document records (for hash dedupe), and per-source sync state."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    title TEXT,
    kind TEXT NOT NULL DEFAULT 'doc',
    content_hash TEXT NOT NULL,
    updated_at TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE TABLE IF NOT EXISTS sync_state (
    source_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (source_id, key)
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    messages_json TEXT NOT NULL DEFAULT '[]',
    est_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON chat_sessions(project_id);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)
        # One shared connection used from API threadpool workers and scheduler
        # threads; the lock keeps each execute+commit pair atomic across threads.
        self._conn = sqlite3.connect(workspace / "catalog.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- sources ------------------------------------------------------------
    def upsert_source(self, source_id: str, name: str, type_: str, options: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sources (id, name, type, config_json, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,
                                                 config_json=excluded.config_json""",
                (source_id, name, type_, json.dumps(options or {}), _now()),
            )
            self._conn.commit()

    def list_sources(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.*, COUNT(d.doc_id) AS doc_count
                   FROM sources s LEFT JOIN documents d ON d.source_id = s.id
                   GROUP BY s.id ORDER BY s.name"""
            ).fetchall()
        return [dict(r) for r in rows]

    # -- documents ----------------------------------------------------------
    def get_document_hash(self, doc_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return row["content_hash"] if row else None

    def upsert_document(
        self,
        doc_id: str,
        source_id: str,
        uri: str,
        title: str | None,
        kind: str,
        content_hash: str,
        updated_at: str | None,
        chunk_count: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO documents (doc_id, source_id, uri, title, kind, content_hash, updated_at, chunk_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                       source_id=excluded.source_id, uri=excluded.uri, title=excluded.title,
                       kind=excluded.kind, content_hash=excluded.content_hash,
                       updated_at=excluded.updated_at, chunk_count=excluded.chunk_count""",
                (doc_id, source_id, uri, title, kind, content_hash, updated_at or _now(), chunk_count),
            )
            self._conn.commit()

    def delete_document(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            docs = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(chunk_count), 0) AS chunks FROM documents"
            ).fetchone()
            sources = self._conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
        return {"sources": sources["n"], "documents": docs["n"], "chunks": docs["chunks"]}

    # -- projects & chat sessions --------------------------------------------
    def upsert_project(self, project_id: str, name: str, description: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO projects (id, name, description, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description""",
                (project_id, name, description, _now()),
            )
            self._conn.commit()

    def get_project(self, id_or_name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ? OR name = ?", (id_or_name, id_or_name)
            ).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT p.*, COUNT(s.id) AS session_count
                   FROM projects p LEFT JOIN chat_sessions s ON s.project_id = p.id
                   GROUP BY p.id ORDER BY p.name"""
            ).fetchall()
        return [dict(r) for r in rows]

    def save_session(
        self,
        session_id: str,
        project_id: str | None,
        title: str,
        summary: str,
        messages_json: str,
        est_tokens: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO chat_sessions (id, project_id, title, summary, messages_json,
                                              est_tokens, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       project_id=excluded.project_id, title=excluded.title,
                       summary=excluded.summary, messages_json=excluded.messages_json,
                       est_tokens=excluded.est_tokens, updated_at=excluded.updated_at""",
                (session_id, project_id, title, summary, messages_json, est_tokens, _now(), _now()),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, project_id: str | None = None) -> list[dict]:
        """Session rows without the (potentially large) message payload."""
        query = (
            "SELECT id, project_id, title, summary, est_tokens, created_at, updated_at "
            "FROM chat_sessions"
        )
        params: tuple = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- users & auth tokens --------------------------------------------------
    def create_user(self, username: str, password_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )
            self._conn.commit()

    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, created_at FROM users ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return row["n"]

    def save_token(self, token_hash: str, username: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO auth_tokens (token_hash, username, created_at) VALUES (?, ?, ?)",
                (token_hash, username, _now()),
            )
            self._conn.commit()

    def get_token_user(self, token_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT username FROM auth_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return row["username"] if row else None

    def delete_token(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
            self._conn.commit()

    def delete_source(self, source_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            self._conn.commit()

    # -- sync state ---------------------------------------------------------
    def get_sync_state(self, source_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM sync_state WHERE source_id = ?", (source_id,)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_sync_state(self, source_id: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sync_state (source_id, key, value) VALUES (?, ?, ?)
                   ON CONFLICT(source_id, key) DO UPDATE SET value=excluded.value""",
                (source_id, key, value),
            )
            self._conn.commit()
