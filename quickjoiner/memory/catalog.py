"""SQL catalog: workspace config, connector sources, document records (hash dedupe),
sync state, projects/sessions, and users/tokens.

`_SqlCatalog` holds the backend-neutral SQL (identical across engines — both SQLite and
Postgres support `ON CONFLICT ... excluded`). `Catalog` is the on-prem SQLite adapter;
`PostgresCatalog` (memory/pg_catalog.py) is the cloud adapter. Both are chosen at runtime
by `memory/factory.py` on `DATABASE_URL`. SQL uses `?` placeholders; each adapter's
primitives convert as needed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Portable across SQLite and Postgres: TEXT/INTEGER, DEFAULT, PRIMARY KEY, UNIQUE,
# CREATE INDEX IF NOT EXISTS, and integer flags for shared/configured.
_SCHEMA_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}', owner TEXT,
        shared INTEGER NOT NULL DEFAULT 1, configured INTEGER NOT NULL DEFAULT 0,
        sync_interval_minutes INTEGER, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS documents (
        doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, uri TEXT NOT NULL, title TEXT,
        kind TEXT NOT NULL DEFAULT 'doc', content_hash TEXT NOT NULL, updated_at TEXT,
        chunk_count INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id)",
    """CREATE TABLE IF NOT EXISTS sync_state (
        source_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (source_id, key))""",
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', messages_json TEXT NOT NULL DEFAULT '[]',
        est_tokens INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_project ON chat_sessions(project_id)",
    """CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created_at TEXT NOT NULL)""",
    # knowledge graph (docs/KNOWLEDGE_GRAPH.md): entities + typed edges, every edge
    # carrying the doc that proves it so graph answers stay citable.
    """CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        source_id TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS entity_aliases (
        alias TEXT NOT NULL, entity_id TEXT NOT NULL,
        PRIMARY KEY (alias, entity_id))""",
    "CREATE INDEX IF NOT EXISTS idx_aliases_entity ON entity_aliases(entity_id)",
    """CREATE TABLE IF NOT EXISTS edges (
        src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
        evidence_doc_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (src, rel, dst, evidence_doc_id))""",
    "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)",
    "CREATE INDEX IF NOT EXISTS idx_edges_evidence ON edges(evidence_doc_id)",
    # knowledge-debt backlog (docs/plans/01): every refusal is logged here; clusters
    # are computed on read (quickjoiner/gaps.py). query is '' in hash-only privacy mode.
    """CREATE TABLE IF NOT EXISTS gaps (
        id TEXT PRIMARY KEY, query TEXT NOT NULL DEFAULT '', query_hash TEXT NOT NULL,
        best_score REAL NOT NULL DEFAULT 0, nearest_json TEXT NOT NULL DEFAULT '[]',
        session_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
        resolution TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_gaps_status ON gaps(status)",
    # A row here means this document's deferred graph work (LLM triple extraction,
    # queued and resolved after the main ingest loop — see pipeline.py) has not yet
    # been persisted. content_hash is written as soon as a document's fast/deterministic
    # work (chunk, embed, store) finishes, independently of when its slow LLM-derived
    # edges land; without this table, a killed/crashed backfill would leave those docs
    # with a hash that already matches their content, so a later incremental sync would
    # see them as "unchanged" and skip them forever, never retrying the graph work.
    """CREATE TABLE IF NOT EXISTS graph_pending (
        doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL DEFAULT '')""",
    # One row per sync run (written at start, updated when it ends) so "what has been
    # happening?" survives a page reload AND a server restart — SyncManager's job map is
    # in-memory and per-process, which is enough to *watch* a run but not to remember it.
    # This is what the notification menu reads.
    """CREATE TABLE IF NOT EXISTS sync_events (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL,
        clean INTEGER NOT NULL DEFAULT 0, stats_json TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT,
        kind TEXT NOT NULL DEFAULT 'sync')""",
    "CREATE INDEX IF NOT EXISTS idx_sync_events_started ON sync_events(started_at)",
    # Per-question chat file attachments (context for one question). Deliberately NOT in the
    # documents/vector memory — these never enter the Uploads connector or the knowledge graph.
    # A row outlives its files: cleanup sets deleted_at (7-day retention) and removes the bytes,
    # but the row stays so history can still show the filename + when it was deleted.
    """CREATE TABLE IF NOT EXISTS context_attachments (
        id TEXT PRIMARY KEY, session_id TEXT, filename TEXT NOT NULL,
        content_type TEXT NOT NULL DEFAULT '', size_bytes INTEGER NOT NULL DEFAULT 0,
        char_count INTEGER NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL, deleted_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_context_attachments_uploaded ON context_attachments(uploaded_at)",
]

# Columns added to tables that already exist in the wild. Applied best-effort on every
# open (an "already exists" error is the expected no-op) — plain ALTER, no IF NOT EXISTS,
# because SQLite doesn't support that form.
_MIGRATION_STATEMENTS = [
    "ALTER TABLE sources ADD COLUMN owner TEXT",
    "ALTER TABLE sources ADD COLUMN shared INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE sources ADD COLUMN configured INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sources ADD COLUMN sync_interval_minutes INTEGER",
    "ALTER TABLE sync_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'sync'",
    # RBAC (docs/plans/08): a user's role gates which parts of the API they may use.
    # Existing single-user workspaces get 'admin' so nobody is locked out by the upgrade.
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'",
    # Graph-extractor version per doc (2026-07-23): lets a plain sync rebuild the knowledge
    # graph for a re-fetched doc when the DETERMINISTIC extractors changed but the content
    # didn't — without re-embedding. Existing docs default 0 (< current) so the next sync
    # that re-provides them refreshes their graph once, then settles.
    "ALTER TABLE documents ADD COLUMN graph_version INTEGER NOT NULL DEFAULT 0",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SqlCatalog:
    """Backend-neutral catalog SQL. Subclasses implement the three primitives."""

    workspace: Path | None = None  # SQLite sets a Path (for one-time YAML migration); PG leaves None

    # -- primitives (implemented per backend) --------------------------------
    def _write(self, sql: str, params: tuple = ()) -> None:
        raise NotImplementedError

    def _read_one(self, sql: str, params: tuple = ()) -> dict | None:
        raise NotImplementedError

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- workspace config -----------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        row = self._read_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def load_config(self):
        """Load the workspace Config, migrating a legacy config.yaml once (SQLite only)."""
        from quickjoiner.config import Config

        raw = self.get_setting("config")
        if raw is None:
            legacy = self.workspace / "config.yaml" if self.workspace else None
            if legacy and legacy.exists():
                import yaml

                data = yaml.safe_load(legacy.read_text(encoding="utf-8")) or {}
                config = Config.model_validate(data)
                self.save_config(config)
                legacy.rename(self.workspace / "config.yaml.migrated")
            else:
                config = Config()
                self.save_config(config)
            return config
        config = Config.model_validate(json.loads(raw))  # blob excludes sources
        config.sources = self.list_source_configs()
        return config

    def save_config(self, config) -> None:
        """Persist Config: settings blob + reconciled connector sources."""
        blob = config.model_dump(mode="json", exclude={"sources"})
        self.set_setting("config", json.dumps(blob))
        keep = set()
        for source in config.sources:
            self.write_source(source)
            keep.add(f"{source.type}:{source.name}")
        rows = self._read_all("SELECT id FROM sources WHERE configured = 1")
        for r in rows:
            if r["id"] not in keep:
                self._write("DELETE FROM sources WHERE id = ?", (r["id"],))

    def write_source(self, source) -> None:
        """Full upsert of a configured connector (sets owner/shared/configured=1)."""
        sid = f"{source.type}:{source.name}"
        self._write(
            """INSERT INTO sources (id, name, type, config_json, owner, shared,
                                    configured, sync_interval_minutes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, config_json=excluded.config_json,
                   owner=excluded.owner, shared=excluded.shared, configured=1,
                   sync_interval_minutes=excluded.sync_interval_minutes""",
            (sid, source.name, source.type, json.dumps(source.options), source.owner,
             1 if source.shared else 0, source.sync_interval_minutes, _now()),
        )

    def list_source_configs(self) -> list:
        """Reconstruct configured-connector SourceConfigs from the sources table."""
        from quickjoiner.config import SourceConfig

        rows = self._read_all(
            "SELECT name, type, config_json, owner, shared, sync_interval_minutes "
            "FROM sources WHERE configured = 1 ORDER BY name"
        )
        return [
            SourceConfig(
                name=r["name"], type=r["type"], options=json.loads(r["config_json"]),
                owner=r["owner"], shared=bool(r["shared"]),
                sync_interval_minutes=r["sync_interval_minutes"],
            )
            for r in rows
        ]

    # -- sources --------------------------------------------------------------
    def upsert_source(self, source_id: str, name: str, type_: str, options: dict | None = None) -> None:
        """Register/refresh a source during ingest (sync, webhook, taught notes).

        Ownership fields (owner/shared/configured) are set only by write_source /
        save_config; this preserves them so a sync doesn't reset a source to commons.
        """
        self._write(
            """INSERT INTO sources (id, name, type, config_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,
                                             config_json=excluded.config_json""",
            (source_id, name, type_, json.dumps(options or {}), _now()),
        )
        self._declare_aka_aliases(source_id, options)

    def _declare_aka_aliases(self, source_id: str, options: dict | None) -> None:
        """Persist a connector's declared "also known as" names (the `aka` option,
        comma-separated string or list) as aliases on its source entity — feeding
        `resolve_entity`, alias query expansion, autocomplete, AND the same_as identity
        bridges (a declared alias is exactly the identity claim name-matching can't
        discover: repo "Stevedore" aka "appriver.provisioning"). Runs at every sync
        start via upsert_source, so edits apply on the next sync. Best-effort — an
        alias failure must never break registering the source."""
        raw = (options or {}).get("aka")
        if not raw:
            return
        try:
            from quickjoiner.ingest.pipeline import source_entity

            akas = raw if isinstance(raw, list) else str(raw).split(",")
            akas = [a.strip() for a in akas if a and a.strip()]
            if not akas:
                return
            eid, ename, kind = source_entity(source_id)
            # The entity may not exist until the first document lands — create it now so
            # the aliases resolve immediately (gc keeps it once real edges cite it).
            self.upsert_entity(eid, ename, kind, source_id)
            for alias in akas:
                self.add_entity_alias(alias, eid)
        except Exception:  # noqa: BLE001
            pass

    def list_sources(self) -> list[dict]:
        return self._read_all(
            """SELECT s.*, COUNT(d.doc_id) AS doc_count
               FROM sources s LEFT JOIN documents d ON d.source_id = s.id
               GROUP BY s.id ORDER BY s.name"""
        )

    def delete_source(self, source_id: str) -> None:
        self._write("DELETE FROM sources WHERE id = ?", (source_id,))

    # -- documents ------------------------------------------------------------
    def get_document_hash(self, doc_id: str) -> str | None:
        row = self._read_one("SELECT content_hash FROM documents WHERE doc_id = ?", (doc_id,))
        return row["content_hash"] if row else None

    def get_document_graph_version(self, doc_id: str) -> int:
        """The graph-extractor version this doc's edges were last built with (0 if never
        recorded / pre-migration). The pipeline refreshes the graph when this is below the
        current `GRAPH_EXTRACTOR_VERSION` even though the content is unchanged."""
        row = self._read_one("SELECT graph_version FROM documents WHERE doc_id = ?", (doc_id,))
        return int(row["graph_version"]) if row and row["graph_version"] is not None else 0

    def set_document_graph_version(self, doc_id: str, version: int) -> None:
        self._write("UPDATE documents SET graph_version = ? WHERE doc_id = ?", (version, doc_id))

    def get_document(self, doc_id: str) -> dict | None:
        return self._read_one("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))

    def documents_for_source(self, source_id: str) -> list[dict]:
        return self._read_all(
            "SELECT * FROM documents WHERE source_id = ? ORDER BY uri", (source_id,)
        )

    def upsert_document(self, doc_id, source_id, uri, title, kind, content_hash,
                        updated_at, chunk_count, graph_version: int = 0) -> None:
        self._write(
            """INSERT INTO documents (doc_id, source_id, uri, title, kind, content_hash, updated_at, chunk_count, graph_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                   source_id=excluded.source_id, uri=excluded.uri, title=excluded.title,
                   kind=excluded.kind, content_hash=excluded.content_hash,
                   updated_at=excluded.updated_at, chunk_count=excluded.chunk_count,
                   graph_version=excluded.graph_version""",
            (doc_id, source_id, uri, title, kind, content_hash, updated_at or _now(),
             chunk_count, graph_version),
        )

    def delete_document(self, doc_id: str) -> None:
        self._write("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self._write("DELETE FROM edges WHERE evidence_doc_id = ?", (doc_id,))
        self._write("DELETE FROM graph_pending WHERE doc_id = ?", (doc_id,))

    def delete_documents_for_source(self, source_id: str) -> int:
        """Remove every document of a source plus the graph edges / pending rows those
        documents produced. Entities are left to `gc_orphan_entities` because a node may
        be shared with other sources. Returns the number of documents removed."""
        doc_ids = [r["doc_id"] for r in self._read_all(
            "SELECT doc_id FROM documents WHERE source_id = ?", (source_id,))]
        for doc_id in doc_ids:
            self.delete_document(doc_id)  # cascades edges + graph_pending
        return len(doc_ids)

    def gc_orphan_entities(self) -> int:
        """Drop entities that participate in no edge — dangling graph nodes left behind
        after a purge/resync — and their aliases, keeping the graph's invariant that
        every node is part of at least one (cited) relationship. Returns count removed.
        A node that a re-sync re-asserts with an edge simply comes back.

        `same_as` bridges are NOT evidence of existence (they are name-equality
        inferences with no citing document — see ingest/bridges.py), so they neither
        keep a node alive here nor survive their endpoints: dangling bridges are swept
        after the orphans go."""
        orphan_ids = [r["id"] for r in self._read_all(
            "SELECT id FROM entities WHERE id NOT IN "
            "(SELECT src FROM edges WHERE rel <> 'same_as' "
            " UNION SELECT dst FROM edges WHERE rel <> 'same_as')")]
        for eid in orphan_ids:
            self._write("DELETE FROM entity_aliases WHERE entity_id = ?", (eid,))
            self._write("DELETE FROM entities WHERE id = ?", (eid,))
        if orphan_ids:
            self._write(
                "DELETE FROM edges WHERE rel = 'same_as' AND ("
                "src NOT IN (SELECT id FROM entities) OR "
                "dst NOT IN (SELECT id FROM entities))")
        return len(orphan_ids)

    def refresh_same_as_bridges(self) -> int:
        """Recompute the deterministic cross-source identity bridges (ingest/bridges.py)
        from the current entities table + their aliases (incl. the connectors' declared
        "also known as" names). Full replace — the bridge layer is derived, so
        recomputing after each ingest batch keeps it consistent with whatever entities
        exist now (bridges to deleted entities simply don't come back). Returns the
        number of bridges now in place."""
        from quickjoiner.ingest.bridges import compute_same_as_bridges

        entities = self._read_all("SELECT id, name, type FROM entities")
        alias_rows = self._read_all("SELECT entity_id, alias FROM entity_aliases")
        bridges = compute_same_as_bridges(
            entities, [(r["entity_id"], r["alias"]) for r in alias_rows])
        self._write("DELETE FROM edges WHERE rel = 'same_as'")
        for src, rel, dst, detail in bridges:
            self._write(
                "INSERT INTO edges (src, rel, dst, evidence_doc_id, detail) "
                "VALUES (?, ?, ?, '', ?) "
                "ON CONFLICT (src, rel, dst, evidence_doc_id) DO NOTHING",
                (src, rel, dst, detail),
            )
        return len(bridges)

    def clear_sync_state(self, source_id: str) -> None:
        """Forget a source's sync watermark so the next sync is a full pull."""
        self._write("DELETE FROM sync_state WHERE source_id = ?", (source_id,))

    def reset_knowledge(self, include_gaps: bool = True) -> dict[str, int]:
        """Global memory reset: wipe everything ingested/derived, leaving the workspace as
        if nothing had ever synced — while KEEPING connector configs, users, chat
        sessions/projects, settings, and the sync-event history.

        Removes: all documents, the whole knowledge graph (edges, entities, aliases,
        deferred graph work), and every sync watermark (so the next sync is a full pull).
        Also drops the ingestion-bucket source rows (taught notes, distilled conversations,
        webhook pushes — `configured=0`); configured connectors stay listed at 0 documents.
        The gaps backlog is cleared by default (it references now-deleted near-miss docs).
        The caller must wipe the vector store separately (`store.reset()`).

        Portable `?`-SQL in the neutral base ⇒ both backends. Returns counts for the UI."""
        counts = {
            "documents": self._read_one("SELECT COUNT(*) AS n FROM documents")["n"],
            "entities": self._read_one("SELECT COUNT(*) AS n FROM entities")["n"],
            "edges": self._read_one("SELECT COUNT(*) AS n FROM edges")["n"],
        }
        self._write("DELETE FROM documents")
        self._write("DELETE FROM edges")
        self._write("DELETE FROM entities")
        self._write("DELETE FROM entity_aliases")
        self._write("DELETE FROM graph_pending")
        self._write("DELETE FROM sync_state")
        # Ingestion buckets exist only because of ingested content — drop them; keep real
        # connectors so they stay listed (at 0 docs) and re-syncable.
        self._write("DELETE FROM sources WHERE configured = 0")
        if include_gaps:
            self._write("DELETE FROM gaps")
        return counts

    # -- deferred graph work (see graph_pending's comment in the schema) --------
    def mark_graph_pending(self, doc_id: str, source_id: str) -> None:
        self._write(
            "INSERT INTO graph_pending (doc_id, source_id) VALUES (?, ?) "
            "ON CONFLICT(doc_id) DO NOTHING",
            (doc_id, source_id),
        )

    def clear_graph_pending(self, doc_id: str) -> None:
        self._write("DELETE FROM graph_pending WHERE doc_id = ?", (doc_id,))

    def is_graph_pending(self, doc_id: str) -> bool:
        return self._read_one("SELECT 1 FROM graph_pending WHERE doc_id = ?", (doc_id,)) is not None

    def count_graph_pending(self, source_id: str | None = None) -> int:
        if source_id is None:
            row = self._read_one("SELECT COUNT(*) AS n FROM graph_pending")
        else:
            row = self._read_one("SELECT COUNT(*) AS n FROM graph_pending WHERE source_id = ?", (source_id,))
        return int(row["n"]) if row else 0

    def stats(self) -> dict:
        docs = self._read_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(chunk_count), 0) AS chunks FROM documents"
        )
        sources = self._read_one("SELECT COUNT(*) AS n FROM sources")
        return {
            "sources": int(sources["n"]), "documents": int(docs["n"]),
            "chunks": int(docs["chunks"]),
        }

    # -- projects & chat sessions ---------------------------------------------
    def upsert_project(self, project_id: str, name: str, description: str = "") -> None:
        self._write(
            """INSERT INTO projects (id, name, description, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description""",
            (project_id, name, description, _now()),
        )

    def get_project(self, id_or_name: str) -> dict | None:
        return self._read_one(
            "SELECT * FROM projects WHERE id = ? OR name = ?", (id_or_name, id_or_name)
        )

    def list_projects(self) -> list[dict]:
        return self._read_all(
            """SELECT p.*, COUNT(s.id) AS session_count
               FROM projects p LEFT JOIN chat_sessions s ON s.project_id = p.id
               GROUP BY p.id ORDER BY p.name"""
        )

    def save_session(self, session_id, project_id, title, summary, messages_json, est_tokens) -> None:
        self._write(
            """INSERT INTO chat_sessions (id, project_id, title, summary, messages_json,
                                          est_tokens, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   project_id=excluded.project_id, title=excluded.title,
                   summary=excluded.summary, messages_json=excluded.messages_json,
                   est_tokens=excluded.est_tokens, updated_at=excluded.updated_at""",
            (session_id, project_id, title, summary, messages_json, est_tokens, _now(), _now()),
        )

    def get_session(self, session_id: str) -> dict | None:
        return self._read_one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))

    def list_sessions(self, project_id: str | None = None) -> list[dict]:
        query = ("SELECT id, project_id, title, summary, est_tokens, created_at, updated_at "
                 "FROM chat_sessions")
        params: tuple = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY updated_at DESC"
        return self._read_all(query, params)

    def delete_session(self, session_id: str) -> None:
        self._write("DELETE FROM chat_sessions WHERE id = ?", (session_id,))

    def delete_sessions(self, project_id: str | None = None) -> int:
        """Delete every session (optionally scoped to a project). Returns the count."""
        if project_id:
            rows = self._read_all("SELECT id FROM chat_sessions WHERE project_id = ?", (project_id,))
            self._write("DELETE FROM chat_sessions WHERE project_id = ?", (project_id,))
        else:
            rows = self._read_all("SELECT id FROM chat_sessions", ())
            self._write("DELETE FROM chat_sessions", ())
        return len(rows)

    # -- per-question chat attachments (context files, NOT memory) -------------
    def add_context_attachment(self, att_id: str, filename: str, content_type: str,
                               size_bytes: int, char_count: int, uploaded_at: str,
                               session_id: str | None = None) -> None:
        self._write(
            "INSERT INTO context_attachments (id, session_id, filename, content_type, "
            "size_bytes, char_count, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (att_id, session_id, filename, content_type, size_bytes, char_count, uploaded_at),
        )

    def get_context_attachment(self, att_id: str) -> dict | None:
        return self._read_one("SELECT * FROM context_attachments WHERE id = ?", (att_id,))

    def get_context_attachments(self, ids: list[str]) -> list[dict]:
        """The rows for a set of attachment ids (order not guaranteed). Used to resolve the
        CURRENT state (deleted_at) of attachments referenced by a stored chat message."""
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return self._read_all(f"SELECT * FROM context_attachments WHERE id IN ({ph})", tuple(ids))

    def link_context_attachments(self, ids: list[str], session_id: str) -> None:
        """Bind attachments to the session they were first used in (they may be uploaded
        before the session exists). Only sets a still-null session_id — never reassigns."""
        for att_id in ids:
            self._write(
                "UPDATE context_attachments SET session_id = ? WHERE id = ? AND session_id IS NULL",
                (session_id, att_id),
            )

    def list_expired_context_attachments(self, cutoff: str) -> list[dict]:
        """Live (not-yet-deleted) attachments uploaded before `cutoff` — the scheduler's
        7-day sweep list."""
        return self._read_all(
            "SELECT * FROM context_attachments WHERE deleted_at IS NULL AND uploaded_at < ?",
            (cutoff,),
        )

    def mark_context_attachment_deleted(self, att_id: str, deleted_at: str) -> None:
        self._write(
            "UPDATE context_attachments SET deleted_at = ? WHERE id = ?", (deleted_at, att_id))

    # -- users & auth tokens --------------------------------------------------
    def create_user(self, username: str, password_hash: str, role: str = "viewer") -> None:
        self._write(
            "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, ?)",
            (username, password_hash, _now(), role),
        )

    def get_user(self, username: str) -> dict | None:
        return self._read_one("SELECT * FROM users WHERE username = ?", (username,))

    def set_user_role(self, username: str, role: str) -> None:
        self._write("UPDATE users SET role = ? WHERE username = ?", (role, username))

    def list_users(self) -> list[dict]:
        return self._read_all("SELECT username, created_at, role FROM users ORDER BY username")

    def count_users(self) -> int:
        row = self._read_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"])

    def save_token(self, token_hash: str, username: str) -> None:
        self._write(
            "INSERT INTO auth_tokens (token_hash, username, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(token_hash) DO UPDATE SET username=excluded.username, created_at=excluded.created_at",
            (token_hash, username, _now()),
        )

    def get_token_user(self, token_hash: str) -> str | None:
        row = self._read_one("SELECT username FROM auth_tokens WHERE token_hash = ?", (token_hash,))
        return row["username"] if row else None

    def delete_token(self, token_hash: str) -> None:
        self._write("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))

    # -- knowledge graph --------------------------------------------------------
    def upsert_entity(self, entity_id: str, name: str, type_: str, source_id: str = "") -> None:
        self._write(
            """INSERT INTO entities (id, name, type, source_id) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type""",
            (entity_id, name, type_, source_id),
        )

    def entities_by_type(self, type_: str) -> list[dict]:
        """All entities of one type — candidate pool for entity-resolution dedup
        (ingest/entity_resolution.py), which only ever compares same-typed entities."""
        return self._read_all("SELECT * FROM entities WHERE type = ? ORDER BY id", (type_,))

    def add_entity_alias(self, alias: str, entity_id: str) -> None:
        self._write(
            "INSERT INTO entity_aliases (alias, entity_id) VALUES (?, ?) "
            "ON CONFLICT(alias, entity_id) DO NOTHING",
            (alias.strip().lower(), entity_id),
        )

    def replace_doc_edges(self, evidence_doc_id: str, edges: list[tuple]) -> None:
        """All edges asserted by one evidence document, replace-on-reingest:
        the edge set follows the document's lifecycle (delete_document cascades)."""
        self._write("DELETE FROM edges WHERE evidence_doc_id = ?", (evidence_doc_id,))
        for src, rel, dst, detail in edges:
            self._write(
                """INSERT INTO edges (src, rel, dst, evidence_doc_id, detail)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(src, rel, dst, evidence_doc_id)
                   DO UPDATE SET detail=excluded.detail""",
                (src, rel, dst, evidence_doc_id, detail),
            )

    def resolve_entity(self, text: str) -> dict | None:
        """Entity by id, exact name, or alias — case-insensitive, so the org's
        spoken forms ("nautical models") resolve like the real id."""
        needle = text.strip().lower()
        if not needle:
            return None
        row = self._read_one(
            "SELECT * FROM entities WHERE id = ? OR LOWER(name) = ?", (needle, needle)
        )
        if row:
            return row
        return self._read_one(
            """SELECT e.* FROM entities e JOIN entity_aliases a ON a.entity_id = e.id
               WHERE a.alias = ?""",
            (needle,),
        )

    def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """Entity autocomplete: name/alias substring match (case-insensitive),
        ranked by how connected the entity is. Powers the graph view's
        search-as-you-type — a lighter-weight sibling to resolve_entity's exact
        id/name/alias lookup, for "what might they mean" instead of "resolve this"."""
        needle = f"%{query.strip().lower()}%"
        if needle == "%%":
            return []
        return self._read_all(
            """SELECT e.id, e.name, e.type,
                      (SELECT COUNT(*) FROM edges g WHERE g.src = e.id OR g.dst = e.id) AS degree
               FROM entities e
               WHERE LOWER(e.name) LIKE ?
                  OR e.id IN (SELECT entity_id FROM entity_aliases WHERE alias LIKE ?)
               ORDER BY degree DESC LIMIT ?""",
            (needle, needle, limit),
        )

    def bridge_entities(self, limit: int = 20) -> list[dict]:
        """Entities touched by edges whose evidence documents come from more than
        one distinct source — the graph's actual cross-source correlation, and a
        far more useful "where do I start?" list than an arbitrary graph slice.
        Ordered by how many sources touch it, then by degree."""
        return self._read_all(
            """SELECT e.id, e.name, e.type,
                      COUNT(DISTINCT d.source_id) AS source_count,
                      COUNT(*) AS degree
               FROM entities e
               JOIN (
                   SELECT src AS entity_id, evidence_doc_id FROM edges
                   UNION ALL
                   SELECT dst AS entity_id, evidence_doc_id FROM edges
               ) touch ON touch.entity_id = e.id
               JOIN documents d ON d.doc_id = touch.evidence_doc_id
               GROUP BY e.id, e.name, e.type
               HAVING COUNT(DISTINCT d.source_id) > 1
               ORDER BY source_count DESC, degree DESC
               LIMIT ?""",
            (limit,),
        )

    _EDGE_SELECT = """SELECT g.src, g.rel, g.dst, g.detail, g.evidence_doc_id,
                             s.name AS src_name, s.type AS src_type,
                             t.name AS dst_name, t.type AS dst_type,
                             d.title AS evidence_title, d.uri AS evidence_uri,
                             d.kind AS evidence_kind
                      FROM edges g
                      LEFT JOIN entities s ON s.id = g.src
                      LEFT JOIN entities t ON t.id = g.dst
                      LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id"""

    def graph_neighbors(self, entity_id: str) -> list[dict]:
        """Every edge touching the entity, with far-node names and the evidence
        document's title/uri joined in (for citations)."""
        return self._read_all(
            self._EDGE_SELECT + " WHERE g.src = ? OR g.dst = ? ORDER BY g.rel, g.dst",
            (entity_id, entity_id),
        )

    def edge_corroboration(self, src: str, rel: str, dst: str) -> dict:
        """How many distinct evidence docs, and distinct sources, assert one exact
        edge — the corroboration inputs to plan 06's score_edge. No new storage:
        the (src, rel, dst, evidence_doc_id) PK already keeps one row per
        corroborating document."""
        row = self._read_one(
            """SELECT COUNT(DISTINCT g.evidence_doc_id) AS doc_count,
                      COUNT(DISTINCT d.source_id) AS source_count
               FROM edges g LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
               WHERE g.src = ? AND g.rel = ? AND g.dst = ? AND g.evidence_doc_id <> ''""",
            (src, rel, dst),
        )
        return {"doc_count": (row or {}).get("doc_count") or 0,
                "source_count": (row or {}).get("source_count") or 0}

    def entity_evidence(self, entity_id: str, limit: int = 3) -> list[dict]:
        """Titles/kinds of the evidence docs behind edges touching this entity —
        the context the entity-resolution adjudicator judges merges from
        (plan 06 §1.D: bare name strings alone made the LLM default to NONE)."""
        return self._read_all(
            """SELECT DISTINCT d.title, d.kind FROM edges g
               JOIN documents d ON d.doc_id = g.evidence_doc_id
               WHERE g.src = ? OR g.dst = ? ORDER BY d.title LIMIT ?""",
            (entity_id, entity_id, limit),
        )

    def graph_path(self, src_id: str, dst_id: str, max_hops: int = 3) -> list[dict] | None:
        """Shortest chain of edges linking two entities (undirected BFS, hop-capped),
        each hop carrying names + evidence. [] if src == dst; None if unconnected
        within reach — the tool reports that as not-learned, never invents a link.

        Unbounded on purpose: this loads the full edge table into an in-memory
        adjacency dict (fast — tens of thousands of rows is sub-second Python, not
        an LLM payload), but a "no known path" answer is treated everywhere as an
        honest refusal, not a hedge. A silent row cap here would have meant that
        refusal could be wrong — reporting "not learned" for a connection that
        exists just outside the truncated set. Correctness over a hypothetical
        save that was never the actual bottleneck."""
        if src_id == dst_id:
            return []
        rows = self._read_all(self._EDGE_SELECT)
        adjacency: dict[str, list[dict]] = {}
        for r in rows:
            adjacency.setdefault(r["src"], []).append(r)
            adjacency.setdefault(r["dst"], []).append(r)

        from collections import deque

        came_from: dict[str, tuple[str, dict]] = {}
        seen = {src_id}
        frontier = deque([(src_id, 0)])
        while frontier:
            node, hops = frontier.popleft()
            if hops >= max_hops:
                continue
            for edge in adjacency.get(node, ()):
                nxt = edge["dst"] if edge["src"] == node else edge["src"]
                if nxt in seen:
                    continue
                seen.add(nxt)
                came_from[nxt] = (node, edge)
                if nxt == dst_id:
                    path: list[dict] = []
                    cur = nxt
                    while cur != src_id:
                        prev_node, via = came_from[cur]
                        path.append(via)
                        cur = prev_node
                    return list(reversed(path))
                frontier.append((nxt, hops + 1))
        return None

    def graph_path_candidates(self, src_id: str, dst_id: str, max_hops: int = 3,
                              max_candidates: int = 3) -> list[list[dict]]:
        """Up to `max_candidates` MATERIALLY DIFFERENT chains linking two entities,
        in nondecreasing hop order — so a 1-hop claim from meeting notes and a 3-hop
        chain through an architecture doc both surface instead of shortest silently
        winning (plan 06's motivating failure). `graph_path` (singular) is untouched;
        this is an additive sibling and candidate 0 always agrees with it.

        Bounded simple-path BFS (k-shortest-paths relaxation of "visited"): each
        non-destination node may be expanded up to `max_candidates` times, so
        alternate routes survive where plain BFS would prune them. Two chains are
        materially different iff their signature — (frozenset of intermediate node
        ids, frozenset of rels, frozenset of evidence classes) — differs; hop count
        is deliberately NOT a component (same-route chains of equal shape are
        duplicates, and shortness alone is not a distinct answer). Same-signature
        chains keep the first (shortest) representative.

        Reachability posture matches graph_path's "unbounded on purpose" note: the
        full edge table is loaded, and the first path found pops through exactly the
        states plain node-BFS would settle, so candidate 0 exists iff graph_path
        finds a path — the search bounds (per-node expansion cap, 20k popped states,
        raw cap 4*max_candidates) only ever trim EXTRA candidates, never turn a real
        connection into a false "no path"."""
        if src_id == dst_id:
            return []
        rows = self._read_all(self._EDGE_SELECT)
        adjacency: dict[str, list[dict]] = {}
        for r in rows:
            adjacency.setdefault(r["src"], []).append(r)
            adjacency.setdefault(r["dst"], []).append(r)

        from collections import defaultdict, deque

        # memory/ stays import-light toward agent/: lazy import keeps layering one-way.
        from quickjoiner.agent.confidence import classify_evidence

        raw: list[list[dict]] = []
        raw_cap = 4 * max_candidates
        budget = 20_000
        expansions: dict[str, int] = defaultdict(int)
        frontier: deque = deque([(src_id, [], {src_id})])
        while frontier and budget > 0 and len(raw) < raw_cap:
            budget -= 1
            node, path, on_path = frontier.popleft()
            if len(path) >= max_hops:
                continue
            for edge in adjacency.get(node, ()):
                nxt = edge["dst"] if edge["src"] == node else edge["src"]
                if nxt in on_path:
                    continue  # simple paths only
                if nxt == dst_id:
                    raw.append(path + [edge])
                    if len(raw) >= raw_cap:
                        break
                elif expansions[nxt] < max_candidates:
                    expansions[nxt] += 1
                    frontier.append((nxt, path + [edge], on_path | {nxt}))

        def signature(chain: list[dict]) -> tuple:
            nodes, cur = [], src_id
            for hop in chain:
                cur = hop["dst"] if hop["src"] == cur else hop["src"]
                nodes.append(cur)
            intermediates = frozenset(nodes[:-1])  # strictly between src and dst
            rels = frozenset(hop["rel"] for hop in chain)
            classes = frozenset(
                classify_evidence(hop.get("evidence_title") or "",
                                  hop.get("evidence_uri") or "",
                                  hop.get("evidence_kind") or "")
                for hop in chain
            )
            return (intermediates, rels, classes)

        out: list[list[dict]] = []
        seen_sigs: set = set()
        for chain in raw:  # BFS order == nondecreasing hop count
            sig = signature(chain)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            out.append(chain)
            if len(out) >= max_candidates:
                break
        return out

    def graph_snapshot(self, entity_id: str | None = None, limit: int = 400) -> dict:
        """Nodes + edges for /api/graph: one entity's neighborhood, or the whole
        graph capped at `limit` edges.

        The whole-graph case is sampled *fairly across entity TYPES* (and across
        src within a type), not just the alphabetically-first `limit` rows. Two
        failure modes this avoids: (1) a plain `ORDER BY src LIMIT n` lets one
        high-degree entity consume the whole budget (renders as a single star);
        (2) a per-src cap alone still front-loads whichever type sorts first and
        is numerous — after a GitLab sync the hundreds of `branch:` entities
        (sorting before every other type) ate the entire 400-edge budget via
        belongs_to/for_ticket, hiding the services/deps/deploys/code that are
        fully present. So each src_type gets an even share (`per_type_cap`), and
        within a type a round-robin over src spreads that share across many
        entities — a representative multi-type view at any `limit`."""
        if entity_id:
            rows = self.graph_neighbors(entity_id)[:limit]
        else:
            # Balance the sample across entity TYPES, then across src within a type. A plain
            # per-src cap still front-loads whichever type sorts first alphabetically and is
            # numerous — e.g. after GitLab sync the 329 `branch:` entities (sorting before
            # every other type) consumed the entire 400-edge budget via belongs_to/for_ticket,
            # rendering the whole graph as branches+tickets and hiding services/deps/deploys/
            # code that are fully present. So: each src_type gets an even share of the budget
            # (per_type_cap), and within a type the round-robin over src (rn_src) spreads it
            # across many entities rather than one dominant node.
            tcount = self._read_one(
                "SELECT COUNT(DISTINCT s.type) AS n FROM edges g LEFT JOIN entities s ON s.id = g.src"
            )
            n_types = max(1, (tcount["n"] if tcount else 0) or 1)
            per_type_cap = max(5, limit // n_types)
            rows = self._read_all(
                f"""SELECT src, rel, dst, detail, evidence_doc_id,
                           src_name, src_type, dst_name, dst_type,
                           evidence_title, evidence_uri, evidence_kind
                    FROM (
                        SELECT src, rel, dst, detail, evidence_doc_id, src_name, src_type,
                               dst_name, dst_type, evidence_title, evidence_uri, evidence_kind,
                               ROW_NUMBER() OVER (
                                   PARTITION BY src_type ORDER BY rn_src, src, rel, dst
                               ) AS rn_type
                        FROM (
                            SELECT g.src, g.rel, g.dst, g.detail, g.evidence_doc_id,
                                   s.name AS src_name, s.type AS src_type,
                                   t.name AS dst_name, t.type AS dst_type,
                                   d.title AS evidence_title, d.uri AS evidence_uri,
                                   d.kind AS evidence_kind,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY g.src ORDER BY g.rel, g.dst
                                   ) AS rn_src
                            FROM edges g
                            LEFT JOIN entities s ON s.id = g.src
                            LEFT JOIN entities t ON t.id = g.dst
                            LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
                        ) capped
                    ) typed
                    WHERE rn_type <= ?
                    ORDER BY src_type, rn_type
                    LIMIT ?""",
                (per_type_cap, limit),
            )
        nodes: dict[str, dict] = {}
        edges = []
        for r in rows:
            nodes.setdefault(r["src"], {"id": r["src"], "name": r["src_name"] or r["src"],
                                        "type": r["src_type"] or "unknown"})
            nodes.setdefault(r["dst"], {"id": r["dst"], "name": r["dst_name"] or r["dst"],
                                        "type": r["dst_type"] or "unknown"})
            edges.append({
                "src": r["src"], "rel": r["rel"], "dst": r["dst"], "detail": r["detail"],
                "evidence": {"doc_id": r["evidence_doc_id"], "title": r["evidence_title"],
                             "uri": r["evidence_uri"], "kind": r["evidence_kind"]},
            })
        if entity_id and entity_id not in nodes:
            ent = self._read_one("SELECT * FROM entities WHERE id = ?", (entity_id,))
            if ent:
                nodes[entity_id] = {"id": ent["id"], "name": ent["name"], "type": ent["type"]}
        return {"nodes": list(nodes.values()), "edges": edges}

    def graph_expand(self, seed_doc_ids: list[str], limit: int = 5) -> list[dict]:
        """Documents one graph hop from the seed documents: the entities the seeds
        evidence, then OTHER documents that evidence edges touching those entities.
        This is the graph-expansion retrieval channel — it surfaces cross-source
        evidence the vector search missed. Each row carries the relation, the two
        entity names, and the related document's title/uri for citation."""
        if not seed_doc_ids:
            return []
        dph = ",".join("?" for _ in seed_doc_ids)
        seed_entities = [
            r["e"] for r in self._read_all(
                f"SELECT src AS e FROM edges WHERE evidence_doc_id IN ({dph}) "
                f"UNION SELECT dst AS e FROM edges WHERE evidence_doc_id IN ({dph})",
                (*seed_doc_ids, *seed_doc_ids),
            )
        ]
        if not seed_entities:
            return []
        # Cross the deterministic identity bridges (ingest/bridges.py): a seed doc that
        # evidences `service:connector` should also expand through `repo:connector`.
        # Only the seed SET grows — the bridges themselves carry no evidence doc, so the
        # `evidence_doc_id <> ''` filter below still guarantees every returned row is a
        # real, citable document; the bridge is never itself presented as evidence.
        sph = ",".join("?" for _ in seed_entities)
        partners = [
            r["e"] for r in self._read_all(
                f"SELECT dst AS e FROM edges WHERE rel = 'same_as' AND src IN ({sph}) "
                f"UNION SELECT src AS e FROM edges WHERE rel = 'same_as' AND dst IN ({sph})",
                (*seed_entities, *seed_entities),
            )
        ]
        seed_entities = list(dict.fromkeys([*seed_entities, *partners]))
        eph = ",".join("?" for _ in seed_entities)
        rows = self._read_all(
            f"""SELECT g.rel, g.evidence_doc_id AS doc_id, g.detail,
                       s.name AS src_name, t.name AS dst_name,
                       d.title AS title, d.uri AS uri
                FROM edges g
                LEFT JOIN entities s ON s.id = g.src
                LEFT JOIN entities t ON t.id = g.dst
                LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
                WHERE (g.src IN ({eph}) OR g.dst IN ({eph}))
                  AND g.evidence_doc_id <> ''
                  AND g.evidence_doc_id NOT IN ({dph})
                ORDER BY g.rel, g.dst""",
            (*seed_entities, *seed_entities, *seed_doc_ids),
        )
        seen: set[str] = set()
        out: list[dict] = []
        for r in rows:
            if r["doc_id"] in seen:
                continue
            seen.add(r["doc_id"])
            out.append(r)
            if len(out) >= limit:
                break
        return out

    # -- knowledge-debt backlog (gaps) ------------------------------------------
    def log_gap(self, query: str, best_score: float, nearest: list,
                session_id: str = "", store_query: bool = True) -> None:
        """Record one refusal. `query` is stored only when store_query is True
        (hash-only privacy mode otherwise); query_hash is always present so
        hash-only gaps still dedupe/cluster by exact normalized query."""
        normalized = " ".join((query or "").lower().split())
        query_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        self._write(
            """INSERT INTO gaps (id, query, query_hash, best_score, nearest_json,
                                 session_id, status, resolution, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', '', ?)""",
            (uuid.uuid4().hex, query if store_query else "", query_hash,
             float(best_score), json.dumps(nearest), session_id, _now()),
        )

    def list_gaps(self, status: str = "open") -> list[dict]:
        return self._read_all(
            "SELECT * FROM gaps WHERE status = ? ORDER BY created_at, id", (status,)
        )

    def resolve_gaps(self, gap_ids: list[str], resolution: str) -> None:
        for gid in gap_ids:
            self._write(
                "UPDATE gaps SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                (resolution, _now(), gid),
            )

    # -- sync history (notifications) ----------------------------------------
    def record_sync_event(self, job_id: str, source: str, state: str, clean: bool,
                          started_at: str, ended_at: str | None = None,
                          stats: dict | None = None, error: str | None = None,
                          kind: str = "sync") -> None:
        """Upsert one job run (`kind` = sync | cleanup). Called twice per job (start, then
        end), keyed on the job id so the finished row replaces the running one instead of
        duplicating it."""
        self._write(
            """INSERT INTO sync_events (id, source, state, clean, stats_json, error,
                                        started_at, ended_at, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                   stats_json=excluded.stats_json, error=excluded.error,
                   ended_at=excluded.ended_at""",
            (job_id, source, state, 1 if clean else 0,
             json.dumps(stats) if stats else "", error or "", started_at, ended_at, kind),
        )

    def list_sync_events(self, since: str, limit: int = 50) -> list[dict]:
        """Sync runs started at/after `since` (ISO-8601 UTC), newest first."""
        return self._read_all(
            "SELECT * FROM sync_events WHERE started_at >= ? "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (since, int(limit)),
        )

    def prune_sync_events(self, before: str) -> int:
        """Drop *finished* runs older than `before` — this table is a rolling window, not an
        audit log. Unfinished rows (ended_at IS NULL) are never pruned: a deliberately-paused
        sync must survive the laptop being closed for longer than the retention window so it
        can still be resumed on the next startup (see `list_unfinished_syncs`)."""
        rows = self._read_all(
            "SELECT id FROM sync_events WHERE started_at < ? AND ended_at IS NOT NULL", (before,))
        self._write(
            "DELETE FROM sync_events WHERE started_at < ? AND ended_at IS NOT NULL", (before,))
        return len(rows)

    def list_unfinished_syncs(self) -> list[dict]:
        """Every sync run with no end recorded (`ended_at IS NULL`) — the rows a process left
        behind when it exited. A `paused` one was a deliberate hold and is resumable after a
        restart; a `running`/`stopping` one died mid-flight and is finalized as interrupted.
        Drives `SyncManager.revive_paused`."""
        return self._read_all(
            "SELECT * FROM sync_events WHERE ended_at IS NULL ORDER BY started_at")

    # -- sync state -----------------------------------------------------------
    def get_sync_state(self, source_id: str) -> dict[str, str]:
        rows = self._read_all("SELECT key, value FROM sync_state WHERE source_id = ?", (source_id,))
        return {r["key"]: r["value"] for r in rows}

    def set_sync_state(self, source_id: str, key: str, value: str) -> None:
        self._write(
            "INSERT INTO sync_state (source_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(source_id, key) DO UPDATE SET value=excluded.value",
            (source_id, key, value),
        )


class Catalog(_SqlCatalog):
    """On-prem SQLite adapter. One shared connection guarded by a lock so API
    threadpool and scheduler threads share it safely."""

    def __init__(self, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self._conn = sqlite3.connect(workspace / "catalog.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        for stmt in _SCHEMA_STATEMENTS:
            self._conn.execute(stmt)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to pre-existing DBs."""
        for ddl in _MIGRATION_STATEMENTS:
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
