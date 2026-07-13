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

    def upsert_document(self, doc_id, source_id, uri, title, kind, content_hash,
                        updated_at, chunk_count) -> None:
        self._write(
            """INSERT INTO documents (doc_id, source_id, uri, title, kind, content_hash, updated_at, chunk_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                   source_id=excluded.source_id, uri=excluded.uri, title=excluded.title,
                   kind=excluded.kind, content_hash=excluded.content_hash,
                   updated_at=excluded.updated_at, chunk_count=excluded.chunk_count""",
            (doc_id, source_id, uri, title, kind, content_hash, updated_at or _now(), chunk_count),
        )

    def delete_document(self, doc_id: str) -> None:
        self._write("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self._write("DELETE FROM edges WHERE evidence_doc_id = ?", (doc_id,))

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

    # -- users & auth tokens --------------------------------------------------
    def create_user(self, username: str, password_hash: str) -> None:
        self._write(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, _now()),
        )

    def get_user(self, username: str) -> dict | None:
        return self._read_one("SELECT * FROM users WHERE username = ?", (username,))

    def list_users(self) -> list[dict]:
        return self._read_all("SELECT username, created_at FROM users ORDER BY username")

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

    _EDGE_SELECT = """SELECT g.src, g.rel, g.dst, g.detail, g.evidence_doc_id,
                             s.name AS src_name, s.type AS src_type,
                             t.name AS dst_name, t.type AS dst_type,
                             d.title AS evidence_title, d.uri AS evidence_uri
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

    def graph_path(self, src_id: str, dst_id: str, max_hops: int = 3) -> list[dict] | None:
        """Shortest chain of edges linking two entities (undirected BFS, hop-capped),
        each hop carrying names + evidence. [] if src == dst; None if unconnected
        within reach — the tool reports that as not-learned, never invents a link."""
        if src_id == dst_id:
            return []
        rows = self._read_all(self._EDGE_SELECT + " LIMIT 10000")
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

    def graph_snapshot(self, entity_id: str | None = None, limit: int = 400) -> dict:
        """Nodes + edges for /api/graph: one entity's neighborhood, or the whole
        graph capped at `limit` edges."""
        if entity_id:
            rows = self.graph_neighbors(entity_id)[:limit]
        else:
            rows = self._read_all(self._EDGE_SELECT + " ORDER BY g.src, g.rel LIMIT ?", (limit,))
        nodes: dict[str, dict] = {}
        edges = []
        for r in rows:
            nodes.setdefault(r["src"], {"id": r["src"], "name": r["src_name"] or r["src"],
                                        "type": r["src_type"] or "unknown"})
            nodes.setdefault(r["dst"], {"id": r["dst"], "name": r["dst_name"] or r["dst"],
                                        "type": r["dst_type"] or "unknown"})
            edges.append({
                "src": r["src"], "rel": r["rel"], "dst": r["dst"], "detail": r["detail"],
                "evidence": {"doc_id": r["evidence_doc_id"],
                             "title": r["evidence_title"], "uri": r["evidence_uri"]},
            })
        if entity_id and entity_id not in nodes:
            ent = self._read_one("SELECT * FROM entities WHERE id = ?", (entity_id,))
            if ent:
                nodes[entity_id] = {"id": ent["id"], "name": ent["name"], "type": ent["type"]}
        return {"nodes": list(nodes.values()), "edges": edges}

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
        for ddl in (
            "ALTER TABLE sources ADD COLUMN owner TEXT",
            "ALTER TABLE sources ADD COLUMN shared INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE sources ADD COLUMN configured INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sources ADD COLUMN sync_interval_minutes INTEGER",
        ):
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
