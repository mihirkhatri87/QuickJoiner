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
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG_KEY = "config"  # settings row holding the (sparse) workspace config blob
_DEFAULTS_EPOCH_KEY = "config_defaults_epoch"  # config.DEFAULTS_EPOCH last reconciled

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
    # Environment values a skill needs, layered: `owner` is a username for a personal
    # secret and '' for a workspace-wide one, so one table serves both and a personal
    # value simply wins the lookup. Values are ENCRYPTED (see skills/secrets.py) — unlike
    # connector options, these are individual people's own credentials.
    """CREATE TABLE IF NOT EXISTS skill_secrets (
        owner TEXT NOT NULL, key TEXT NOT NULL, value_enc TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY (owner, key))""",
    # QuickJoiner's OWN definition of a skill, seeded from the folder on first sight but
    # authoritative thereafter — so what a skill requires is configurable here rather than
    # only in a file the uploader wrote. `scope` is 'open' (no credentials, everyone) or
    # 'user' (each person supplies `required_env` before it will run for them).
    """CREATE TABLE IF NOT EXISTS skills (
        name TEXT PRIMARY KEY, path TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT 'open', required_env TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1, installed_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
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
    # graph_json holds the deterministic assertions computed alongside the deferred LLM
    # triples, so a drain (below) can finish an aged-out document's graph faithfully.
    """CREATE TABLE IF NOT EXISTS graph_pending (
        doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL DEFAULT '',
        graph_json TEXT NOT NULL DEFAULT '')""",
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
        char_count INTEGER NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL, deleted_at TEXT,
        extract_error TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_context_attachments_uploaded ON context_attachments(uploaded_at)",
    # User-declared labels over ingested documents (2026-07-25). ONE table covers all three
    # granularities via `uri_prefix`, which is why folders behave the way people expect:
    #   ''            -> the whole connector
    #   'file:///d/docs/arch/'  -> that folder — documents ingested LATER inherit it, because
    #                     the label is a prefix RULE resolved at query time, not a snapshot
    #                     copied onto the rows that happened to exist when you tagged
    #   a full uri    -> exactly one document
    # `kind` splits the two jobs: 'tag' is a filter/scoping label; 'aka' is an alternate name
    # that feeds the existing alias query-expansion path (memory/expansion.py), so calling a
    # deck "the Zix roadmap" finds it without the words appearing in the file.
    """CREATE TABLE IF NOT EXISTS doc_labels (
        source_id TEXT NOT NULL, uri_prefix TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'tag', value TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (source_id, uri_prefix, kind, value))""",
    "CREATE INDEX IF NOT EXISTS idx_doc_labels_source ON doc_labels(source_id)",
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
    # Why an attachment yielded no text (image-only deck, missing parser, corrupt file).
    # Without it a file that extracted to nothing was invisible to the agent, which then
    # flailed instead of telling the user plainly that it could not read their file.
    "ALTER TABLE context_attachments ADD COLUMN extract_error TEXT NOT NULL DEFAULT ''",
    # Small connector-supplied JSON blob for UI display only (e.g. ADO work-item type/state/
    # team/sprint/parent id) — deliberately separate from graph metadata (which becomes
    # entities/edges, not stored raw). Default '{}' so every pre-existing document and every
    # connector that never sets Document.metadata["display"] reads as an empty dict, not null.
    "ALTER TABLE documents ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
    # The deterministic half of a document's deferred graph work (2026-07-30). Rows that
    # predate this column drain with only what their stored text still yields — stated
    # plainly in the drain's log rather than passed off as a complete rebuild.
    "ALTER TABLE graph_pending ADD COLUMN graph_json TEXT NOT NULL DEFAULT ''",
    # Deterministic architectural layer for a code entity (2026-08-10, AI_ROADMAP #29) —
    # api/service/data/ui/utility/infra plus test and vendor, derived from the defining
    # file's own path by ingest/layers.py, never by an LLM. '' means the path said
    # nothing, which is the honest majority case; see that module for why the ambiguous
    # role words are deliberately left untagged.
    "ALTER TABLE entities ADD COLUMN layer TEXT NOT NULL DEFAULT ''",
    # The connector's OWN structural graph claims for a document (2026-08-06), kept so the
    # graph can be rebuilt from stored state without a connector round-trip. These edges —
    # ADO dev-links and work-item hierarchy, GitLab MR/branch joins, Jira issue links,
    # Octopus deployments — are computed while FETCHING, so skipping the fetch is exactly
    # what loses them: measured on a real 100k-edge graph, 27% of all edges could not be
    # re-derived from stored text. Default '' means "never captured", which `regraph`
    # reports as text-only rather than silently treating as "this document has no edges".
    "ALTER TABLE documents ADD COLUMN graph_json TEXT NOT NULL DEFAULT ''",
    # Expression index for resolve_entity's case-insensitive name lookup (2026-07-31,
    # found by `qj bench`). Without it `WHERE id = ? OR LOWER(name) = ?` cannot use an
    # index for the second branch, so every call SCANNED the whole entities table —
    # 36,203 rows on the live workspace, ~30 times per query (alias expansion slides
    # 1-4-token windows), which measured as 27% of total retrieval latency. Portable:
    # both SQLite and Postgres support indexes on an expression, and it must spell
    # LOWER(name) exactly as the query does for the planner to match it.
    "CREATE INDEX IF NOT EXISTS idx_entities_lower_name ON entities(LOWER(name))",
    # Promotion of a personal document to the organisation (2026-08-10, knowledge scopes
    # Y1.8f). Status is '' for the overwhelming majority — a document nobody has offered —
    # so this is a sparse flag on an existing table rather than its own; a promoted
    # document carries no lasting status at all, because its moved source_id IS the record.
    "ALTER TABLE documents ADD COLUMN promotion_status TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN promotion_by TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN promotion_note TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN promotion_at TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_documents_promotion ON documents(promotion_status)",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def label_applies(uri: str, uri_prefix: str) -> bool:
    """Whether a label attached at `uri_prefix` covers a document at `uri` (pure).

    An empty prefix is the whole connector. This is what makes a folder label live: it is
    evaluated against each document's uri at query time, so files ingested after the label
    was created are covered without anyone re-tagging anything.
    """
    return not uri_prefix or uri.startswith(uri_prefix)


def _like_prefix(prefix: str) -> str:
    """`prefix` as a SQL LIKE pattern, with the wildcards a real path may contain escaped —
    a Windows path or a URL can legitimately hold `%` or `_`, which would otherwise silently
    widen the match."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


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

        raw = self.get_setting(_CONFIG_KEY)
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
        # Blob excludes sources, and (since it is stored sparsely) any field the user
        # never set — those pick up whatever default config.py currently ships.
        config = Config.model_validate(self._adopt_shipped_defaults(json.loads(raw)))
        config.sources = self.list_source_configs()
        return config

    def _adopt_shipped_defaults(self, blob: dict) -> dict:
        """Once per workspace: drop values that are only a superseded shipped default.

        A blob written before sparse persistence has every field materialised, pinning
        whatever default was in force when the workspace first saved (see
        config.SUPERSEDED_DEFAULTS for the full reasoning). This prunes exactly those
        values so the current default applies, leaves real customisations alone, and
        LOGS every field it moves — a workspace's retrieval behaviour should never
        change silently. Runs again if DEFAULTS_EPOCH is bumped for a new entry.
        """
        from quickjoiner.config import DEFAULTS_EPOCH, reconcile_superseded_defaults

        try:
            done = int(self.get_setting(_DEFAULTS_EPOCH_KEY) or 0)
        except ValueError:
            done = 0
        if done >= DEFAULTS_EPOCH:
            return blob
        pruned, adopted = reconcile_superseded_defaults(blob)
        for path, was, now in adopted:
            log.info(
                "config: %s was pinned to the superseded default %r; adopting the "
                "shipped default %r (change it in Settings to keep %r)", path, was, now, was
            )
        if adopted:
            self.set_setting(_CONFIG_KEY, json.dumps(pruned))
        self.set_setting(_DEFAULTS_EPOCH_KEY, str(DEFAULTS_EPOCH))
        return pruned

    def save_config(self, config) -> None:
        """Persist Config: settings blob + reconciled connector sources.

        `exclude_defaults` is what lets a retuned default in config.py reach an existing
        workspace: only fields that actually differ from the shipped default are stored,
        so an untouched field stays governed by the code rather than being frozen at
        whatever it happened to be the first time this workspace saved. The trade-off is
        deliberate — a value explicitly set to today's default is indistinguishable from
        an untouched one and will follow a future retune. That matches what the Settings
        drawer already tells the user ("default"), and the alternative is the bug this
        replaces: a shipped retune that silently never applies anywhere.
        """
        blob = config.model_dump(mode="json", exclude={"sources"}, exclude_defaults=True)
        self.set_setting(_CONFIG_KEY, json.dumps(blob))
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

    def set_source_ownership(self, source_id: str, owner: str | None, shared: bool) -> None:
        """Set who owns an INGESTION BUCKET and whether it is shared.

        `write_source`/`save_config` own this for configured connectors; this is the same
        step for the buckets they don't cover — a per-user note bucket has no SourceConfig
        to carry its ownership, and `upsert_source` deliberately preserves (never sets) it.
        """
        self._write(
            "UPDATE sources SET owner = ?, shared = ? WHERE id = ?",
            (owner, 1 if shared else 0, source_id),
        )

    def get_source(self, source_id: str) -> dict | None:
        return self._read_one("SELECT * FROM sources WHERE id = ?", (source_id,))

    def is_private_source(self, source_id: str) -> bool:
        """Is this source readable only by its owner? The ingest-side twin of
        `visible_source_ids` — what the merge guard keys on.

        Private means owned AND not shared, i.e. exactly the rows `visible_source_ids`
        withholds from everyone but their owner. An **unknown or ownerless** source is
        NOT private: ownerless rows are the commons (that method's own rule), and a
        missing row would otherwise silently disable entity resolution org-wide for a
        source that simply hasn't been registered yet. The asymmetry with the read side
        (which fails closed on a missing row) is deliberate: withholding a document is
        cheap and reversible, whereas refusing to merge is a permanent quality loss the
        operator would have no way to notice.
        """
        row = self._read_one(
            "SELECT owner, shared FROM sources WHERE id = ?", (source_id,)
        )
        return bool(row and row["owner"] and not row["shared"])

    def list_sources(self) -> list[dict]:
        return self._read_all(
            """SELECT s.*, COUNT(d.doc_id) AS doc_count
               FROM sources s LEFT JOIN documents d ON d.source_id = s.id
               GROUP BY s.id ORDER BY s.name"""
        )

    def visible_source_ids(self, user: str | None) -> list[str]:
        """Every source id `user` may read — the knowledge-scope key.

        Same rule as `auth.visible`, but over **all** source rows rather than the configured
        connectors `list_source_configs` returns: an ingestion bucket (taught notes, uploads,
        distilled conversations) is a source too, and once a taught note can be private it is
        exactly the row that has to be filtered. Ownerless rows are the commons, so the
        pre-auth corpus keeps reading as commons with no migration.

        Callers only apply this when auth is enabled — see `AppContext.visible_source_ids`,
        which returns None in open mode so single-user behaviour stays byte-identical.
        A source_id with no row here is NOT visible: a filter that fails open is not a filter.
        """
        rows = self._read_all(
            "SELECT id FROM sources WHERE owner IS NULL OR shared = 1 OR owner = ?",
            (user,),
        )
        return [r["id"] for r in rows]

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

    def all_documents(self) -> list[dict]:
        """Every ingested document, grouped by source — the work-list for a whole-workspace
        graph rebuild. Ordered by source so a rebuild can process one source at a time and
        report progress against it."""
        return self._read_all("SELECT * FROM documents ORDER BY source_id, uri")

    def count_documents(self, source_id: str | None = None) -> int:
        """How many documents a graph rebuild would walk — read before starting one, so the
        UI/CLI can say what it is about to do instead of opening an unbounded job."""
        if source_id:
            row = self._read_one(
                "SELECT COUNT(*) AS n FROM documents WHERE source_id = ?", (source_id,))
        else:
            row = self._read_one("SELECT COUNT(*) AS n FROM documents")
        return int(row["n"]) if row else 0

    def documents_missing_graph_payload(self, source_id: str | None = None) -> int:
        """Documents with no stored connector payload — the ones a rebuild can only preserve
        rather than rebuild authoritatively. Surfaced up front so "27% of your edges cannot
        be re-derived" is something the user is told, not something they discover."""
        if source_id:
            row = self._read_one(
                "SELECT COUNT(*) AS n FROM documents WHERE source_id = ? AND graph_json = ''",
                (source_id,))
        else:
            row = self._read_one("SELECT COUNT(*) AS n FROM documents WHERE graph_json = ''")
        return int(row["n"]) if row else 0

    def document_source(self, doc_id: str) -> str | None:
        row = self._read_one("SELECT source_id FROM documents WHERE doc_id = ?", (doc_id,))
        return row["source_id"] if row else None

    # -- labels (tags / aka) over ingested documents --------------------------

    def set_label(self, source_id: str, uri_prefix: str, kind: str, value: str) -> None:
        """Attach a label to a connector (`uri_prefix=''`), a folder (a uri prefix), or one
        document (its full uri). Idempotent."""
        self._write(
            "INSERT INTO doc_labels (source_id, uri_prefix, kind, value, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id, uri_prefix, kind, value) DO NOTHING",
            (source_id, uri_prefix, kind, value.strip(), _now()),
        )

    def remove_label(self, source_id: str, uri_prefix: str, kind: str, value: str) -> None:
        self._write(
            "DELETE FROM doc_labels WHERE source_id = ? AND uri_prefix = ? "
            "AND kind = ? AND value = ?",
            (source_id, uri_prefix, kind, value.strip()),
        )

    def labels_for_source(self, source_id: str) -> list[dict]:
        return self._read_all(
            "SELECT * FROM doc_labels WHERE source_id = ? ORDER BY uri_prefix, kind, value",
            (source_id,),
        )

    def all_labels(self) -> list[dict]:
        """Every label in the workspace — what the scope picker offers."""
        return self._read_all("SELECT * FROM doc_labels ORDER BY kind, value, source_id")

    def delete_labels_for_source(self, source_id: str) -> None:
        self._write("DELETE FROM doc_labels WHERE source_id = ?", (source_id,))

    def resolve_scope(
        self,
        source_ids: list[str] | None = None,
        tags: list[str] | None = None,
        doc_ids: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Turn a user-facing scope selection into `(source_ids, doc_ids)` the stores can filter on.

        A tag on a whole connector resolves to that **source_id**, not to an enumeration of its
        documents — so the common case stays a cheap `source_id IN (...)` predicate however many
        documents the connector holds. Only folder/document-scoped labels enumerate doc ids, and
        those sets are small by construction.
        """
        out_sources = list(dict.fromkeys(source_ids or []))
        out_docs = list(dict.fromkeys(doc_ids or []))
        for tag in tags or []:
            for row in self._read_all(
                "SELECT source_id, uri_prefix FROM doc_labels WHERE kind = 'tag' AND value = ?",
                (tag.strip(),),
            ):
                prefix = row.get("uri_prefix") or ""
                if not prefix:
                    if row["source_id"] not in out_sources:
                        out_sources.append(row["source_id"])
                    continue
                for doc in self._read_all(
                    "SELECT doc_id FROM documents WHERE source_id = ? AND uri LIKE ? ESCAPE '\\'",
                    (row["source_id"], _like_prefix(prefix)),
                ):
                    if doc["doc_id"] not in out_docs:
                        out_docs.append(doc["doc_id"])
        return out_sources, out_docs

    def aka_terms_for_documents(self, doc_ids: list[str]) -> list[str]:
        """`aka` values that apply to any of these documents — the alternate names a user
        declared, surfaced so an answer can explain why a document matched a loose query."""
        terms: list[str] = []
        for doc_id in doc_ids[:50]:
            row = self._read_one(
                "SELECT source_id, uri FROM documents WHERE doc_id = ?", (doc_id,))
            if row is None:
                continue
            for label in self.labels_for_source(row["source_id"]):
                if label.get("kind") != "aka":
                    continue
                if label_applies(row.get("uri") or "", label.get("uri_prefix") or ""):
                    if label["value"] not in terms:
                        terms.append(label["value"])
        return terms

    def upsert_document(self, doc_id, source_id, uri, title, kind, content_hash,
                        updated_at, chunk_count, graph_version: int = 0,
                        metadata_json: str = "{}", graph_json: str = "") -> None:
        self._write(
            """INSERT INTO documents (doc_id, source_id, uri, title, kind, content_hash, updated_at, chunk_count, graph_version, metadata_json, graph_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                   source_id=excluded.source_id, uri=excluded.uri, title=excluded.title,
                   kind=excluded.kind, content_hash=excluded.content_hash,
                   updated_at=excluded.updated_at, chunk_count=excluded.chunk_count,
                   graph_version=excluded.graph_version, metadata_json=excluded.metadata_json,
                   graph_json=excluded.graph_json""",
            (doc_id, source_id, uri, title, kind, content_hash, updated_at or _now(),
             chunk_count, graph_version, metadata_json, graph_json),
        )

    def set_document_graph_payload(self, doc_id: str, graph_json: str) -> None:
        """Store the connector's own structural graph claims for a document.

        Standalone so an UNCHANGED document can have its payload captured on an ordinary
        sync with no re-embed — the backfill path that turns a corpus ingested before this
        column existed into one `regraph` can rebuild faithfully."""
        self._write("UPDATE documents SET graph_json = ? WHERE doc_id = ?", (graph_json, doc_id))

    def edges_for_document(self, doc_id: str) -> list[tuple]:
        """This document's current edges as `(src, rel, dst, detail)` tuples.

        Read back by `regraph` for a document whose connector payload was never captured, so
        the claims it cannot re-derive are carried across the rebuild instead of deleted —
        `replace_doc_edges` is a delete-then-insert, so anything not handed back is gone."""
        rows = self._read_all(
            "SELECT src, rel, dst, detail FROM edges WHERE evidence_doc_id = ?", (doc_id,)
        )
        return [(r["src"], r["rel"], r["dst"], r["detail"] or "") for r in rows]

    def update_document_metadata(self, doc_id: str, metadata_json: str) -> None:
        """Cheap standalone update of a document's display metadata (see
        `Document.metadata["display"]`) with no other side effects — the backfill path for
        an unchanged document whose connector-supplied metadata changed (e.g. a work item's
        team/sprint/state), used alongside the graph-version staleness refresh so both
        self-heal on the next ordinary sync without a re-embed."""
        self._write("UPDATE documents SET metadata_json = ? WHERE doc_id = ?", (metadata_json, doc_id))

    # -- promotion (personal -> organisation) ---------------------------------
    def set_promotion(self, doc_id: str, status: str, by: str = "", note: str = "") -> None:
        """Record (or clear, with `status=''`) a document's standing offer to the org."""
        self._write(
            "UPDATE documents SET promotion_status = ?, promotion_by = ?, "
            "promotion_note = ?, promotion_at = ? WHERE doc_id = ?",
            (status, by, note, _now() if status else "", doc_id),
        )

    def list_promotions(self, status: str = "requested") -> list[dict]:
        """Documents currently offered to the organisation, oldest first — the review
        queue. Joined to their source so a reviewer sees whose note this is without a
        second round trip; ordered oldest-first because a review queue is a backlog, not
        a feed."""
        return self._read_all(
            """SELECT d.*, s.name AS source_name, s.type AS source_type, s.owner AS source_owner
               FROM documents d LEFT JOIN sources s ON s.id = d.source_id
               WHERE d.promotion_status = ? ORDER BY d.promotion_at, d.doc_id""",
            (status,),
        )

    def move_document(self, doc_id: str, source_id: str) -> None:
        """Re-home a document to another source, keeping its `doc_id`.

        The id is a content hash of `(original source_id, uri)`, but it is only DERIVED at
        ingest — everything afterwards treats it as opaque, so a move can keep it and with
        it every citation, edge (`evidence_doc_id`), label and chunk. The caller must move
        the vector rows too (`store.move_document`), since retrieval filters on the chunk's
        own copy of `source_id`.

        One consequence is worth stating rather than discovering: if the ORIGINAL connector
        later re-provides the same uri, it recomputes the old doc_id, finds nothing, and
        ingests a fresh private copy alongside the promoted one. That is the right outcome
        for a re-taught note (the author kept their own), and it is why promotion is a
        reviewed act rather than an automatic one.
        """
        self._write(
            "UPDATE documents SET source_id = ?, promotion_status = '' WHERE doc_id = ?",
            (source_id, doc_id),
        )

    def rehome_document_entities(self, doc_id: str, from_source_id: str,
                                 to_source_id: str) -> int:
        """Move the entities THIS document minted from one source to another.

        `entities.source_id` records who created a node and is what the merge guard and
        the `same_as` bridge pass key on, so promoting a document has to hand over the
        nodes it brought with it. Scoped to entities the document actually cites — a
        promotion must not release the rest of its former bucket's graph — and to nodes
        still marked as the origin's, so an entity that was really the org's all along is
        left alone. Returns how many moved."""
        rows = self._read_all(
            """SELECT DISTINCT e.id FROM entities e
               JOIN edges g ON (g.src = e.id OR g.dst = e.id)
               WHERE g.evidence_doc_id = ? AND e.source_id = ?""",
            (doc_id, from_source_id),
        )
        for row in rows:
            self._write("UPDATE entities SET source_id = ? WHERE id = ?",
                        (to_source_id, row["id"]))
        return len(rows)

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
        number of bridges now in place.

        Entities minted by a **private** source are excluded, because a bridge is the one
        edge with no evidence document — `_evidence_visible` therefore keeps it visible to
        everyone, so bridging a private-only entity to an org one would publish that
        entity's name through `graph_neighbors`/`graph_path` however well its documents
        are filtered. A private source's own entities still bridge among themselves; an
        entity first minted by an org source keeps bridging even when private evidence
        later cites it too (the ownership marker is the minting source, so this errs
        toward a missing bridge rather than a leaked name).
        """
        from quickjoiner.ingest.bridges import compute_same_as_bridges

        entities = self._read_all(
            """SELECT e.id, e.name, e.type FROM entities e
               LEFT JOIN sources s ON s.id = e.source_id
               WHERE s.id IS NULL OR s.owner IS NULL OR s.shared = 1"""
        )
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
    def mark_graph_pending(self, doc_id: str, source_id: str, graph_json: str = "") -> None:
        """`graph_json` carries the DETERMINISTIC assertions computed for this document
        at ingest (the connector's own `metadata["graph"]` — ADO dev-links/hierarchy,
        GitLab MR edges — plus ticket/code/pubsub edges). They are deferred alongside the
        LLM triples rather than written first, so until the pending row resolves the
        document has no edges at all; keeping the payload here means a later **drain**
        can finish the job faithfully instead of re-deriving only what the stored text
        still shows. Re-marking an already-pending document refreshes the payload — the
        newer computation is by definition the better one."""
        self._write(
            "INSERT INTO graph_pending (doc_id, source_id, graph_json) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET source_id = excluded.source_id, "
            "graph_json = excluded.graph_json",
            (doc_id, source_id, graph_json),
        )

    def list_graph_pending(self, source_id: str | None = None,
                           limit: int | None = None) -> list[dict]:
        """Documents whose deferred graph work never landed, joined to what the drain
        needs to redo it without a connector round-trip (uri/title/kind + the stored
        deterministic payload). The JOIN also means a pending row whose document has
        since been deleted simply doesn't appear — `sweep_orphan_graph_pending` removes
        those separately."""
        sql = (
            "SELECT p.doc_id AS doc_id, p.source_id AS source_id, p.graph_json AS graph_json, "
            "d.uri AS uri, d.title AS title, d.kind AS kind "
            "FROM graph_pending p JOIN documents d ON d.doc_id = p.doc_id"
        )
        params: tuple = ()
        if source_id is not None:
            sql += " WHERE p.source_id = ?"
            params = (source_id,)
        sql += " ORDER BY p.source_id, d.uri"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._read_all(sql, params)

    def sweep_orphan_graph_pending(self) -> int:
        """Drop pending rows for documents that no longer exist (deleted source, purged
        document). Returns how many were removed."""
        stale = self._read_all(
            "SELECT doc_id FROM graph_pending WHERE doc_id NOT IN (SELECT doc_id FROM documents)"
        )
        for row in stale:
            self._write("DELETE FROM graph_pending WHERE doc_id = ?", (row["doc_id"],))
        return len(stale)

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

    def graph_pending_by_source(self) -> list[dict]:
        return self._read_all(
            "SELECT source_id, COUNT(*) AS n FROM graph_pending "
            "GROUP BY source_id ORDER BY n DESC"
        )

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
                               session_id: str | None = None, extract_error: str = "") -> None:
        self._write(
            "INSERT INTO context_attachments (id, session_id, filename, content_type, "
            "size_bytes, char_count, uploaded_at, extract_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (att_id, session_id, filename, content_type, size_bytes, char_count, uploaded_at,
             extract_error),
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
    def upsert_entity(self, entity_id: str, name: str, type_: str, source_id: str = "",
                      allow_rename: bool = True, layer: str | None = None) -> None:
        """Insert or update an entity, protecting a well-cased display name.

        `layer` is the deterministic architectural tag from `ingest/layers.py` ('' when
        the evidence says nothing, which is the common case). It is resolved in the same
        statement as the name, by PRECEDENCE rather than last-writer-wins: a production
        layer beats `test` beats `vendor`, and nothing beats a value with ''. A symbol
        defined in both `CustomerService.cs` and `CustomerServiceTests.cs` is a service
        symbol that also happens to be tested — the mirror in the test project should not
        relabel it, and which document a sync happens to reach first must not decide it.

        `allow_rename=False` makes this insert-only: an existing node is left exactly as
        it is and only its absence creates one. That is how evidence from a **private**
        source attaches to an org entity without being able to reshape it — the rename
        rule below is otherwise a second, quieter merge: a document asserting the same id
        under a materially different name renames that node for the whole organisation.
        See `ingest/pipeline._persist_graph`.

        Deterministic extractors (deps.py, code_graph.py, connector metadata) name a
        node once at creation; LLM triple extraction can later propose the SAME id with
        a worse-cased guess ('nautical' for 'Nautical'), which a plain
        `DO UPDATE SET name=excluded.name` silently accepts — degrading the graph view,
        `resolve_entity` output and citations org-wide (found live, plan 05 C4 leg).

        The preference is resolved **inside the statement**, not by reading first: a
        read-then-write would add a round trip per entity per evidence document (the
        hottest write path in a large sync) and would not be atomic — concurrent source
        syncs share one catalog, so two threads could interleave and still clobber.
        Rules, in order: a materially different name (case-insensitively) wins as a
        genuine rename; otherwise a case-only variant loses to an incumbent that carries
        any uppercase; an all-lowercase incumbent is upgraded by the candidate.

        Known backend divergence: SQLite's `lower()` folds ASCII only, Postgres's is
        locale-aware, so a name whose only difference is the case of a NON-ASCII letter
        counts as a rename on SQLite and as a case variant on Postgres. Entity names here
        are repo/package/service identifiers (effectively ASCII), so this is noted rather
        than worked around — doing so would mean a custom collation on both engines."""
        if not allow_rename:
            self._write(
                "INSERT INTO entities (id, name, type, source_id, layer) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (entity_id, name, type_, source_id, layer or ""),
            )
            return
        self._write(
            """INSERT INTO entities (id, name, type, source_id, layer)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = CASE
                          WHEN lower(entities.name) <> lower(excluded.name) THEN excluded.name
                          WHEN entities.name <> lower(entities.name) THEN entities.name
                          ELSE excluded.name
                        END,
                 type = excluded.type,
                 layer = CASE
                           WHEN excluded.layer = '' THEN entities.layer
                           WHEN entities.layer = '' THEN excluded.layer
                           WHEN entities.layer IN ('test', 'vendor')
                                AND excluded.layer NOT IN ('test', 'vendor')
                             THEN excluded.layer
                           WHEN entities.layer = 'vendor' AND excluded.layer = 'test'
                             THEN excluded.layer
                           ELSE entities.layer
                         END""",
            (entity_id, name, type_, source_id, layer or ""),
        )

    def get_entity(self, entity_id: str) -> dict | None:
        """One entity by its exact id — no name/alias fallback, unlike `resolve_entity`.
        The merge guard asks 'which source minted this node', and a question about a
        specific id must not be answered by a same-named different node."""
        return self._read_one("SELECT * FROM entities WHERE id = ?", (entity_id,))

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

    def _visible_entity_filter(self, visible_source_ids: "list[str] | None",
                               column: str = "e.id") -> tuple[str, list]:
        """`(sql_fragment, params)` keeping only entities the asker can actually reach.

        An entity is a NAME — "person:Jane Q" — and a name is disclosure on its own, so
        filtering edges is not enough: autocomplete and the graph search box would still
        offer names mined solely from a private document. Reachable means "at least one
        edge citing a visible document, or an evidence-free derived edge" — the same
        definition `_evidence_visible` uses, so the two can't drift apart.

        Correlated EXISTS rather than a join: `entities` is the larger table and this runs
        per keystroke on the autocomplete path, so it must short-circuit on the first hit.
        """
        if visible_source_ids is None:
            return "", []
        clause, vis = self._evidence_visible(visible_source_ids)
        return (
            f""" AND EXISTS (
                    SELECT 1 FROM edges g
                    LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
                    WHERE (g.src = {column} OR g.dst = {column}){clause})""",
            vis,
        )

    def search_entities(self, query: str, limit: int = 10,
                        visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Entity autocomplete: name/alias substring match (case-insensitive),
        ranked by how connected the entity is. Powers the graph view's
        search-as-you-type — a lighter-weight sibling to resolve_entity's exact
        id/name/alias lookup, for "what might they mean" instead of "resolve this"."""
        needle = f"%{query.strip().lower()}%"
        if needle == "%%":
            return []
        vis_clause, vis = self._visible_entity_filter(visible_source_ids)
        return self._read_all(
            """SELECT e.id, e.name, e.type,
                      (SELECT COUNT(*) FROM edges g WHERE g.src = e.id OR g.dst = e.id) AS degree
               FROM entities e
               WHERE (LOWER(e.name) LIKE ?
                  OR e.id IN (SELECT entity_id FROM entity_aliases WHERE alias LIKE ?))"""
            + vis_clause + " ORDER BY degree DESC LIMIT ?",
            (needle, needle, *vis, limit),
        )

    def bridge_entities(self, limit: int = 20,
                        visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Entities touched by edges whose evidence documents come from more than
        one distinct source — the graph's actual cross-source correlation, and a
        far more useful "where do I start?" list than an arbitrary graph slice.
        Ordered by how many sources touch it, then by degree.

        Filtered evidence changes the ANSWER here, not just the rows: an entity bridging a
        readable source and a private one is a one-source entity as far as this asker is
        concerned, and the `HAVING > 1` must be judged on what they can see.
        """
        clause, vis = self._evidence_visible(visible_source_ids)
        return self._read_all(
            """SELECT e.id, e.name, e.type,
                      COUNT(DISTINCT d.source_id) AS source_count,
                      COUNT(*) AS degree
               FROM entities e
               JOIN (
                   SELECT src AS entity_id, evidence_doc_id FROM edges
                   UNION ALL
                   SELECT dst AS entity_id, evidence_doc_id FROM edges
               ) g ON g.entity_id = e.id
               JOIN documents d ON d.doc_id = g.evidence_doc_id
               WHERE 1 = 1""" + clause + """
               GROUP BY e.id, e.name, e.type
               HAVING COUNT(DISTINCT d.source_id) > 1
               ORDER BY source_count DESC, degree DESC
               LIMIT ?""",
            (*vis, limit),
        )

    _EDGE_SELECT = """SELECT g.src, g.rel, g.dst, g.detail, g.evidence_doc_id,
                             s.name AS src_name, s.type AS src_type, s.layer AS src_layer,
                             t.name AS dst_name, t.type AS dst_type, t.layer AS dst_layer,
                             d.title AS evidence_title, d.uri AS evidence_uri,
                             d.kind AS evidence_kind, d.source_id AS evidence_source_id
                      FROM edges g
                      LEFT JOIN entities s ON s.id = g.src
                      LEFT JOIN entities t ON t.id = g.dst
                      LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id"""

    @staticmethod
    def _evidence_visible(visible_source_ids: "list[str] | None") -> tuple[str, list]:
        """`(sql_fragment, params)` keeping only edges whose evidence document the acting
        user may read — the graph-side twin of `SearchScope`, so an answer can't be assembled
        out of relationships drawn from a source the asker cannot open.

        `None` means no restriction (open mode) and returns an empty fragment, so the
        single-user query text is byte-identical to before. An empty list is unsatisfiable
        and renders `1 = 0` rather than the invalid `IN ()`.

        **An edge with no evidence document survives the filter.** That is not an oversight:
        `same_as` identity bridges are derived from entity names and deliberately carry an
        empty `evidence_doc_id` (never citable, corroboration 0), so they cite nothing and
        can leak no document text. Dropping them would silently degrade the graph for every
        user the moment auth is switched on. An edge whose evidence row is genuinely missing
        joins to NULL and IS filtered out — a visibility filter must fail closed.
        """
        if visible_source_ids is None:
            return "", []
        if not visible_source_ids:
            return " AND 1 = 0", []
        marks = ",".join("?" * len(visible_source_ids))
        return (f" AND (g.evidence_doc_id = '' OR d.source_id IN ({marks}))",
                list(visible_source_ids))

    def graph_neighbors(self, entity_id: str,
                        visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Every edge touching the entity, with far-node names and the evidence
        document's title/uri joined in (for citations)."""
        clause, vis = self._evidence_visible(visible_source_ids)
        return self._read_all(
            self._EDGE_SELECT + " WHERE (g.src = ? OR g.dst = ?)" + clause
            + " ORDER BY g.rel, g.dst",
            (entity_id, entity_id, *vis),
        )

    def graph_relations(self, rel: str, src_type: str | None = None,
                        dst_type: str | None = None, limit: int = 400,
                        visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Every edge of one relation shape, optionally constrained by the entity type on
        each end — the ENUMERATION read the graph could not previously serve.

        `graph_neighbors` answers "what is attached to this one thing" and `graph_path`
        "how do these two connect", but "list every team with its members" is neither: it
        is one relation across the whole graph. Vector search cannot answer it either
        (it returns the top-k most similar chunks, never all-matching-a-filter), so
        without this it took one question per entity.

        Deterministic and complete within `limit`; the caller reports truncation rather
        than presenting a partial list as the whole answer.
        """
        sql = self._EDGE_SELECT + " WHERE g.rel = ?"
        params: list = [rel]
        if src_type:
            sql += " AND s.type = ?"
            params.append(src_type)
        if dst_type:
            sql += " AND t.type = ?"
            params.append(dst_type)
        clause, vis = self._evidence_visible(visible_source_ids)
        sql += clause
        params.extend(vis)
        sql += " ORDER BY t.name, s.name LIMIT ?"
        params.append(max(1, limit))
        return self._read_all(sql, tuple(params))

    def edge_corroboration(self, src: str, rel: str, dst: str,
                           visible_source_ids: "list[str] | None" = None) -> dict:
        """How many distinct evidence docs, and distinct sources, assert one exact
        edge — the corroboration inputs to plan 06's score_edge. No new storage:
        the (src, rel, dst, evidence_doc_id) PK already keeps one row per
        corroborating document.

        Counts only evidence the asker may read: confidence is shown to a person, and a
        number inflated by documents they cannot open would be a claim we can't back up.
        """
        clause, vis = self._evidence_visible(visible_source_ids)
        row = self._read_one(
            """SELECT COUNT(DISTINCT g.evidence_doc_id) AS doc_count,
                      COUNT(DISTINCT d.source_id) AS source_count
               FROM edges g LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
               WHERE g.src = ? AND g.rel = ? AND g.dst = ? AND g.evidence_doc_id <> ''"""
            + clause,
            (src, rel, dst, *vis),
        )
        return {"doc_count": (row or {}).get("doc_count") or 0,
                "source_count": (row or {}).get("source_count") or 0}

    def entity_evidence(self, entity_id: str, limit: int = 3,
                        visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Titles/kinds of the evidence docs behind edges touching this entity —
        the context the entity-resolution adjudicator judges merges from
        (plan 06 §1.D: bare name strings alone made the LLM default to NONE)."""
        # An inner JOIN already excludes evidence-less edges here, so the shared clause's
        # `evidence_doc_id = ''` arm can never widen this read.
        clause, vis = self._evidence_visible(visible_source_ids)
        return self._read_all(
            """SELECT DISTINCT d.title, d.kind FROM edges g
               JOIN documents d ON d.doc_id = g.evidence_doc_id
               WHERE (g.src = ? OR g.dst = ?)""" + clause + " ORDER BY d.title LIMIT ?",
            (entity_id, entity_id, *vis, limit),
        )

    def graph_path(self, src_id: str, dst_id: str, max_hops: int = 3,
                   visible_source_ids: "list[str] | None" = None) -> list[dict] | None:
        """Shortest chain of edges linking two entities (undirected BFS, hop-capped),
        each hop carrying names + evidence. [] if src == dst; None if unconnected
        within reach — the tool reports that as not-learned, never invents a link.

        Unbounded on purpose: this loads the full edge table into an in-memory
        adjacency dict (fast — tens of thousands of rows is sub-second Python, not
        an LLM payload), but a "no known path" answer is treated everywhere as an
        honest refusal, not a hedge. A silent row cap here would have meant that
        refusal could be wrong — reporting "not learned" for a connection that
        exists just outside the truncated set. Correctness over a hypothetical
        save that was never the actual bottleneck. Every edge is still visited;
        only the display columns are deferred to `_hydrate_edges` (see `_edge_scan`),
        so reachability is bit-for-bit what it was."""
        if src_id == dst_id:
            return []
        rows = self._read_all(*self._edge_scan(visible_source_ids))
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
                    return self._hydrate_edges(list(reversed(path)), visible_source_ids)
                frontier.append((nxt, hops + 1))
        return None

    def _edge_scan(self, visible_source_ids: "list[str] | None") -> tuple[str, tuple]:
        """The whole-edge-table read the two path searches share, visibility-filtered.

        Filtering here rather than after the BFS is what keeps a "no known path" answer
        honest: a chain is only reported when EVERY hop rests on evidence the asker may
        read, so the graph never routes an answer through a document they cannot open.

        **Traversal columns only** (`src`/`rel`/`dst`/`evidence_doc_id`) — the names,
        layers and evidence titles a hop is RENDERED with are fetched afterwards by
        `_hydrate_edges`, for the handful of edges that actually end up on a returned
        chain. The whole table is still read (see the reachability note on
        `graph_path`); what changed is how wide each row is. Measured on a synthetic
        graph the size of a real corpus (100k edges / 36k entities / 20k documents),
        where the scan was **92% of a 1.45s `graph_path` call**: pulling 15 columns
        through three LEFT JOINs costs 1333ms, the four traversal columns 242ms — a
        5.5x saving on the dominant term, for a BFS that reads exactly three of those
        columns and throws the other twelve away. The joins are not free per row and
        there are ~100k rows; the chain that comes back has at most three.

        The `documents` join survives only when a visibility filter is active, because
        the predicate itself is over `d.source_id`; in open mode (the single-user
        default) the statement is a bare scan of `edges` with no join at all.

        **`ORDER BY` is load-bearing, not tidiness.** BFS explores neighbours in
        adjacency-insertion order, so when two chains of EQUAL length connect the same
        pair, whichever edge was read first wins — and the previous statement had no
        ordering at all, leaving that choice to the query planner. Measured while
        narrowing the columns: 15 of 166 connected pairs came back down a different
        (equally short, equally valid) route purely because the row order moved. Ordering
        on the primary key `(src, rel, dst, evidence_doc_id)` makes the chosen chain
        reproducible across backends, planners and column lists, which matters for a
        product whose answers cite their hops. It is also **free** — those four columns
        ARE the primary key, so this is a covering index scan: no table lookup, no sort.
        Both path searches share this one statement, which is what keeps
        `graph_path_candidates`' documented "candidate 0 agrees with graph_path"
        invariant true by construction rather than by luck.
        """
        clause, vis = self._evidence_visible(visible_source_ids)
        sql = "SELECT g.src, g.rel, g.dst, g.evidence_doc_id FROM edges g"
        if clause:
            sql += (" LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id"
                    " WHERE 1 = 1" + clause)
        return sql + " ORDER BY g.src, g.rel, g.dst, g.evidence_doc_id", tuple(vis)

    def _hydrate_edges(self, edges: list[dict],
                       visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Re-read the display columns (`*_name`, `*_layer`, `evidence_*`) for edges the
        BFS actually returned, keyed on the `edges` primary key so a row maps to exactly
        one hop. Order-preserving, and the visibility clause is re-applied rather than
        trusted from the scan — belt and braces on a filter whose failure mode is a leak.

        A hop that somehow fails to hydrate keeps its traversal row rather than being
        dropped: a chain is a claim about connectivity, and silently shortening one would
        turn a correct answer into a wrong one. Missing display fields render as blank,
        which is already what a LEFT JOIN against a deleted document produced.
        """
        if not edges:
            return []
        clause, vis = self._evidence_visible(visible_source_ids)
        by_key: dict[tuple, dict] = {}
        keys = [(e["src"], e["rel"], e["dst"], e["evidence_doc_id"]) for e in edges]
        # Chunked so a long chain can't outgrow a backend's bound-parameter limit.
        for i in range(0, len(keys), 40):
            batch = keys[i:i + 40]
            match = " OR ".join(
                ["(g.src = ? AND g.rel = ? AND g.dst = ? AND g.evidence_doc_id = ?)"] * len(batch))
            params = [p for key in batch for p in key] + list(vis)
            for row in self._read_all(
                self._EDGE_SELECT + f" WHERE ({match})" + clause, tuple(params)
            ):
                by_key[(row["src"], row["rel"], row["dst"], row["evidence_doc_id"])] = row
        return [by_key.get(key, edge) for key, edge in zip(keys, edges)]

    def graph_path_candidates(self, src_id: str, dst_id: str, max_hops: int = 3,
                              max_candidates: int = 3,
                              visible_source_ids: "list[str] | None" = None) -> list[list[dict]]:
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
        rows = self._read_all(*self._edge_scan(visible_source_ids))
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
                                  hop.get("evidence_kind") or "",
                                  hop.get("evidence_source_id") or "")
                for hop in chain
            )
            return (intermediates, rels, classes)

        # Hydrate every raw candidate in ONE batch before signing: `signature` reads the
        # evidence class, which is a display column the traversal scan no longer carries.
        # Bounded by construction — `raw_cap` chains of at most `max_hops` hops each.
        flat = self._hydrate_edges([hop for chain in raw for hop in chain], visible_source_ids)
        cursor = 0
        hydrated: list[list[dict]] = []
        for chain in raw:
            hydrated.append(flat[cursor:cursor + len(chain)])
            cursor += len(chain)

        out: list[list[dict]] = []
        seen_sigs: set = set()
        for chain in hydrated:  # BFS order == nondecreasing hop count
            sig = signature(chain)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            out.append(chain)
            if len(out) >= max_candidates:
                break
        return out

    def graph_snapshot(self, entity_id: str | None = None, limit: int = 400,
                       visible_source_ids: "list[str] | None" = None) -> dict:
        """Nodes + edges for /api/graph: one entity's neighborhood, or the whole
        graph capped at `limit` edges.

        The whole-graph case can only ever draw a fraction of a real graph (400 of
        109,003 edges on the live corpus), so *which* fraction is the whole design,
        and the answer arrived in three corrections:

        1. A plain `ORDER BY src LIMIT n` lets one high-degree entity consume the
           whole budget — it renders as a single star.
        2. A per-src cap alone still front-loads whichever entity TYPE sorts first
           and is numerous: after a GitLab sync the hundreds of `branch:` entities
           ate the entire budget via belongs_to/for_ticket, hiding the services,
           deps, deploys and code that were fully present. Hence the even per-type
           share, still applied below.
        3. Both of those pick edges without regard to whether their *other* end is
           also drawn, so the budget fills with dangling leaves. Measured on the live
           109k-edge graph: 656 nodes for 392 edges, **91% of them degree-1** — a field
           of stubs, which is exactly what "nothing is connected" looks like. So a
           **connected core** is chosen first (the best-connected entities, evenly
           across types) and only edges with BOTH ends inside it are returned: 174
           nodes, 309 edges, **40% degree-1**, all 14 entity types still present. The
           budget is a ceiling, not a target — 309 edges that connect read better than
           392 that mostly don't. It is also 2.3x faster (2058ms -> 883ms), because the
           expensive join now runs against a small id list.

        Returns `totals` + `truncated` alongside the sample, so a caller can say what
        it left out instead of presenting a fraction as the whole organization."""
        if entity_id:
            rows = self.graph_neighbors(
                entity_id, visible_source_ids=visible_source_ids)[:limit]
        else:
            # How many entity types there are sets every per-type share below — the
            # even split is what keeps one numerous type (329 `branch:` entities after a
            # GitLab sync) from consuming the whole budget and hiding the rest.
            tcount = self._read_one(
                "SELECT COUNT(DISTINCT s.type) AS n FROM edges g LEFT JOIN entities s ON s.id = g.src"
            )
            n_types = max(1, (tcount["n"] if tcount else 0) or 1)
            # Entities per type to build the core from. Fewer, better-connected entities beat
            # more, barely-connected ones: at `limit` edges the view can only ever show a
            # fraction of a real graph, and a fraction that hangs together is readable while
            # the same budget spread thinner is not.
            per_type_entities = max(6, limit // n_types)
            per_src_cap = max(3, limit // (n_types * 2))
            vis_clause, vis = self._evidence_visible(visible_source_ids)
            # Resolved as its own query rather than a CTE: SQLite re-evaluates a CTE joined
            # twice, which measured 900ms+ on a 109k-edge graph even though computing the
            # degrees alone is 92ms. Two indexed steps with the ids passed in is both faster
            # and portable — no engine-specific MATERIALIZED hint.
            core = [r["id"] for r in self._read_all(
                """SELECT ranked.id FROM (
                       SELECT t.id, ROW_NUMBER() OVER (
                                  PARTITION BY e.type ORDER BY t.d DESC, t.id
                              ) AS rn
                       FROM (
                           SELECT id, SUM(d) AS d FROM (
                               SELECT src AS id, COUNT(*) AS d FROM edges GROUP BY src
                               UNION ALL
                               SELECT dst AS id, COUNT(*) AS d FROM edges GROUP BY dst
                           ) both_ends GROUP BY id
                       ) t JOIN entities e ON e.id = t.id
                   ) ranked WHERE ranked.rn <= ?""",
                (per_type_entities,),
            )]
            if not core:
                return {"nodes": [], "edges": [],
                        "totals": self.graph_totals(visible_source_ids),
                        "truncated": False}
            ph = ",".join("?" for _ in core)
            rows = self._read_all(
                f"""SELECT src, rel, dst, detail, evidence_doc_id,
                           src_name, src_type, src_layer, dst_name, dst_type, dst_layer,
                           evidence_title, evidence_uri, evidence_kind
                    FROM (
                        SELECT g.src, g.rel, g.dst, g.detail, g.evidence_doc_id,
                               s.name AS src_name, s.type AS src_type, s.layer AS src_layer,
                               t.name AS dst_name, t.type AS dst_type, t.layer AS dst_layer,
                               d.title AS evidence_title, d.uri AS evidence_uri,
                               d.kind AS evidence_kind,
                               ROW_NUMBER() OVER (
                                   PARTITION BY g.src ORDER BY g.rel, g.dst
                               ) AS rn_src,
                               ROW_NUMBER() OVER (
                                   PARTITION BY s.type ORDER BY g.src, g.rel, g.dst
                               ) AS rn_type
                        FROM edges g
                        LEFT JOIN entities s ON s.id = g.src
                        LEFT JOIN entities t ON t.id = g.dst
                        LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
                        -- BOTH ends in the core, deliberately. Taking edges that merely
                        -- start in the core fills the budget with dangling leaves: measured
                        -- 91% degree-1 nodes that way versus 40% here, for a picture
                        -- that hangs together. The budget is a ceiling, not a target:
                        -- 309 edges that connect beat 392 that mostly don't.
                        -- (The '%' here is safe only because PostgresCatalog._pg
                        -- escapes it; psycopg would otherwise read it as a placeholder.)
                        WHERE g.src IN ({ph}) AND g.dst IN ({ph}){vis_clause}
                    ) picked
                    WHERE rn_src <= ? AND rn_type <= ?
                    ORDER BY src_type, rn_type
                    LIMIT ?""",
                (*core, *core, *vis, per_src_cap, max(5, limit // n_types), limit),
            )
        nodes: dict[str, dict] = {}
        edges = []
        for r in rows:
            # `layer` is '' for most nodes and that is reported as-is, never guessed: it
            # is only ever set where a defining file's own path stated a role.
            nodes.setdefault(r["src"], {"id": r["src"], "name": r["src_name"] or r["src"],
                                        "type": r["src_type"] or "unknown",
                                        "layer": r.get("src_layer") or ""})
            nodes.setdefault(r["dst"], {"id": r["dst"], "name": r["dst_name"] or r["dst"],
                                        "type": r["dst_type"] or "unknown",
                                        "layer": r.get("dst_layer") or ""})
            edges.append({
                "src": r["src"], "rel": r["rel"], "dst": r["dst"], "detail": r["detail"],
                "evidence": {"doc_id": r["evidence_doc_id"], "title": r["evidence_title"],
                             "uri": r["evidence_uri"], "kind": r["evidence_kind"]},
            })
        if entity_id and entity_id not in nodes:
            ent = self._read_one("SELECT * FROM entities WHERE id = ?", (entity_id,))
            if ent:
                nodes[entity_id] = {"id": ent["id"], "name": ent["name"], "type": ent["type"]}
        # What was left out, stated. A whole-graph view can only ever draw a fraction of a
        # real graph — 400 of 109,003 edges on the live corpus — and returning that fraction
        # with no totals lets it read as the entire organization. Same no-silent-caps rule
        # the crawler and the graph tools follow.
        totals = self.graph_totals(visible_source_ids)
        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "totals": totals,
            "truncated": len(edges) < totals["edges"],
        }

    def graph_totals(self, visible_source_ids: "list[str] | None" = None) -> dict:
        """How big the graph actually is — the denominator for any sampled view.

        Counted over what the asker may read, so the "sample of N" the toolbar renders is
        the size of THEIR graph. A global total here would both overstate their view and
        quietly disclose how much they cannot see.
        """
        clause, vis = self._evidence_visible(visible_source_ids)
        if not clause:
            e = self._read_one("SELECT COUNT(*) AS n FROM edges")
            n = self._read_one("SELECT COUNT(*) AS n FROM entities")
            return {"edges": (e["n"] if e else 0) or 0, "entities": (n["n"] if n else 0) or 0}
        # `_evidence_visible` names `g` and `d`, so the filtered counts join the same way
        # the reads do — one definition of "visible edge", not a second one drifting here.
        base = ("FROM edges g LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id "
                "WHERE 1 = 1" + clause)
        e = self._read_one(f"SELECT COUNT(*) AS n {base}", tuple(vis))
        n = self._read_one(
            f"""SELECT COUNT(*) AS n FROM (
                    SELECT g.src AS id {base} UNION SELECT g.dst AS id {base}
                ) reachable""",
            (*vis, *vis),
        )
        return {"edges": (e["n"] if e else 0) or 0, "entities": (n["n"] if n else 0) or 0}

    def graph_expand(self, seed_doc_ids: list[str], limit: int = 5,
                     visible_source_ids: "list[str] | None" = None) -> list[dict]:
        """Documents one graph hop from the seed documents: the entities the seeds
        evidence, then OTHER documents that evidence edges touching those entities.
        This is the graph-expansion retrieval channel — it surfaces cross-source
        evidence the vector search missed. Each row carries the relation, the two
        entity names, and the related document's title/uri for citation.

        Every row returned here is a citable document, so `visible_source_ids` matters more
        than anywhere else in the graph: this is the one read that hands whole documents back
        to the answer path. The seeds are already visible (they are grounded hits), but their
        one-hop neighbours need not be.
        """
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
        clause, vis = self._evidence_visible(visible_source_ids)
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
                  AND g.evidence_doc_id NOT IN ({dph}){clause}
                ORDER BY g.rel, g.dst""",
            (*seed_entities, *seed_entities, *seed_doc_ids, *vis),
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

    # -- skill secrets --------------------------------------------------------
    # `owner` is a username for a personal value, '' for a workspace-wide one. Values are
    # stored already-encrypted by skills/secrets.py; this layer never sees plaintext and
    # deliberately has no "list every value" read — only a keyed lookup and a key listing,
    # so nothing can accidentally dump the store.

    def set_skill_secret(self, owner: str, key: str, value_enc: str) -> None:
        self._write(
            "INSERT INTO skill_secrets (owner, key, value_enc, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner, key) DO UPDATE SET value_enc=excluded.value_enc, "
            "updated_at=excluded.updated_at",
            (owner or "", key, value_enc, _now()),
        )

    def delete_skill_secret(self, owner: str, key: str) -> None:
        self._write("DELETE FROM skill_secrets WHERE owner=? AND key=?", (owner or "", key))

    def skill_secret_keys(self, owner: str) -> list[str]:
        """Which keys this owner has set — names only, never values."""
        rows = self._read_all(
            "SELECT key FROM skill_secrets WHERE owner=? ORDER BY key", (owner or "",))
        return [r["key"] for r in rows]

    def skill_secrets_for(self, owners: list[str]) -> dict[str, dict[str, str]]:
        """owner -> {key: encrypted value}, for the owners given. Fetched in ONE query so
        resolving a skill's whole environment is a single read rather than one per key."""
        if not owners:
            return {}
        marks = ",".join("?" for _ in owners)
        rows = self._read_all(
            f"SELECT owner, key, value_enc FROM skill_secrets WHERE owner IN ({marks})",
            tuple(owners),
        )
        out: dict[str, dict[str, str]] = {o: {} for o in owners}
        for r in rows:
            out.setdefault(r["owner"], {})[r["key"]] = r["value_enc"]
        return out

    # -- skill definitions ----------------------------------------------------

    def list_skill_configs(self) -> list[dict]:
        return self._read_all("SELECT * FROM skills ORDER BY name")

    def get_skill_config(self, name: str) -> dict | None:
        return self._read_one("SELECT * FROM skills WHERE name = ?", (name,))

    def register_skill(self, name: str, path: str, origin: str, scope: str,
                       required_env: str, installed_by: str = "") -> None:
        """Record a newly-discovered skill. Deliberately does NOT overwrite `scope` or
        `required_env` on conflict: those are the configuration an admin edits in the UI,
        and re-running discovery must not silently revert their decision. Only the
        location follows the folder."""
        now = _now()
        self._write(
            "INSERT INTO skills (name, path, origin, scope, required_env, enabled, "
            "installed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, origin=excluded.origin, "
            "updated_at=excluded.updated_at",
            (name, path, origin, scope, required_env, installed_by, now, now),
        )

    def update_skill_config(self, name: str, scope: str | None = None,
                            required_env: str | None = None,
                            enabled: bool | None = None) -> None:
        sets, params = [], []
        if scope is not None:
            sets.append("scope = ?"); params.append(scope)
        if required_env is not None:
            sets.append("required_env = ?"); params.append(required_env)
        if enabled is not None:
            sets.append("enabled = ?"); params.append(1 if enabled else 0)
        if not sets:
            return
        sets.append("updated_at = ?"); params.append(_now())
        params.append(name)
        self._write(f"UPDATE skills SET {', '.join(sets)} WHERE name = ?", tuple(params))

    def delete_skill_config(self, name: str) -> None:
        self._write("DELETE FROM skills WHERE name = ?", (name,))

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

    def set_sync_state_many(self, source_id: str, state: dict[str, str]) -> None:
        """Persist every key a connector wrote back into the state dict it was handed.

        Until OneDrive, the only watermark any connector needed was the caller-set
        `since` timestamp. Microsoft Graph is different: its incremental contract is an
        opaque **deltaLink** that the *connector* receives and must hand back next time.
        So `sync(state)` may now mutate its `state` dict, and callers commit the result
        alongside `since` — only after a fully successful run, so a failed or stopped
        sync can never advance a delta cursor past documents it did not ingest.
        `clear_sync_state` (clean re-sync) drops these with everything else.
        """
        for key, value in state.items():
            if value is not None:
                self.set_sync_state(source_id, key, str(value))


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
