"""Knowledge store: LanceDB vectors + SQLite FTS5 sidecar for hybrid retrieval.

Search runs two legs and fuses them:
  dense  — cosine over LanceDB (exact below retrieval.ann_min_rows, IVF ANN above;
           ensure_ann_index() builds the index once the corpus is big enough),
  sparse — BM25 over an FTS5 sidecar (<workspace>/fts.db, porter stemming) that
           rescues exact-token matches (error codes, ticket IDs, service names)
           the embedder ranks poorly.
Legs merge with reciprocal rank fusion (memory/hybrid.py); an optional
cross-encoder (memory/reranker.py) reorders the fused head.

Grounding contract: SearchHit.score is ALWAYS the dense cosine score and
retrieval.min_score gates on it, hybrid or not — fusion decides what surfaces
and in which order, never whether the agent may claim it learned something.
Sparse-only candidates get their cosine computed via a targeted vector lookup.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

import lancedb

from quickjoiner.config import RetrievalConfig
from quickjoiner.ingest.normalize import normalize_query
from quickjoiner.memory.embedder import Embedder
from quickjoiner.memory.hybrid import rrf_fuse

_TABLE = "chunks"
_TOKEN = re.compile(r"[A-Za-z0-9_]+")

# LanceDB is copy-on-write: every delete/add/upsert writes a new table version and
# leaves the superseded data files + version manifests on disk until they're pruned.
# Nothing prunes them automatically, so a workspace grows without bound across syncs,
# clean re-syncs and resets (observed live: ~55k dead versions / 54 GB of manifests
# behind a ~250 MB live corpus). `maybe_compact` reclaims them, throttled to fire only
# once this many table versions have accumulated since the last pass — matching
# LanceDB's own "optimize every ~20 modification operations" guidance so a stream of
# small incremental syncs doesn't trigger a full optimize each time.
_COMPACT_EVERY_VERSIONS = 500
_COMPACT_MARKER = ".compacted_version"


@dataclass
class SearchHit:
    text: str
    score: float
    doc_id: str
    source_id: str
    uri: str
    title: str
    kind: str


@dataclass
class SearchScope:
    """Which slice of memory a search may look at.

    Both legs of the hybrid search filter on this **before** ranking, so a scoped question
    genuinely does less work rather than retrieving everything and discarding most of it.
    Empty (the default) means the whole corpus — the unscoped behaviour, untouched.

    `source_ids` and `doc_ids` are OR-ed: "this connector, plus these specific documents".
    A tag is resolved to those two by `catalog.resolve_scope` before it reaches a store, so
    stores never need to know what a label is.
    """

    source_ids: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.source_ids and not self.doc_ids

    def matches(self, source_id: str, doc_id: str) -> bool:
        """Python-side twin of the SQL predicate — used by the FTS leg's safety net and by
        tests, so both engines and both legs agree on one definition of 'in scope'."""
        return self.is_empty() or source_id in self.source_ids or doc_id in self.doc_ids


def _sql_list(values: list[str]) -> str:
    """Quote a list for a LanceDB SQL `IN (...)`. Source ids embed a user-chosen connector
    name, so a stray quote must be escaped rather than trusted into the predicate."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


class KnowledgeStore:
    def __init__(
        self,
        workspace: Path,
        embedder: Embedder,
        retrieval: RetrievalConfig | None = None,
        reranker=None,
    ):
        self._lance_dir = workspace / "lancedb"
        self._db = lancedb.connect(str(self._lance_dir))
        self._embedder = embedder
        self._retrieval = retrieval or RetrievalConfig()
        self._reranker = reranker
        self._fts_lock = threading.Lock()
        self._fts = self._open_fts(workspace)
        self._backfill_fts()

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    # ---------------------------------------------------------------- FTS side
    def _open_fts(self, workspace: Path) -> sqlite3.Connection | None:
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(workspace / "fts.db", check_same_thread=False)
            conn.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                       title, text, id UNINDEXED, doc_id UNINDEXED, source_id UNINDEXED,
                       uri UNINDEXED, kind UNINDEXED, updated_at UNINDEXED,
                       tokenize='porter unicode61')"""
            )
            conn.commit()
            return conn
        except sqlite3.OperationalError:  # sqlite built without FTS5 → dense-only
            return None

    def _backfill_fts(self) -> None:
        """Upgrade path: workspaces indexed before the FTS sidecar existed have
        chunks in LanceDB only. Copy them over once so hybrid works immediately."""
        if self._fts is None:
            return
        table = self._table()
        if table is None:
            return
        with self._fts_lock:
            (count,) = self._fts.execute("SELECT count(*) FROM chunks_fts").fetchone()
            if count > 0:
                return
            try:
                rows = (
                    table.to_arrow()
                    .select(["title", "text", "id", "doc_id", "source_id", "uri", "kind", "updated_at"])
                    .to_pylist()
                )
            except Exception:
                return  # backfill is best-effort; new ingests populate FTS anyway
            self._fts.executemany(
                "INSERT INTO chunks_fts (title, text, id, doc_id, source_id, uri, kind, updated_at) "
                "VALUES (:title, :text, :id, :doc_id, :source_id, :uri, :kind, :updated_at)",
                rows,
            )
            self._fts.commit()

    def _fts_write(self, sql: str, params=()) -> None:
        if self._fts is None:
            return
        with self._fts_lock:
            self._fts.execute(sql, params)
            self._fts.commit()

    @staticmethod
    def _fts_match(query: str) -> str:
        """Build a safe FTS5 MATCH expression: bare quoted tokens OR-ed together
        (BM25 handles the ranking; porter stemming handles inflection)."""
        tokens = _TOKEN.findall(query)[:32]
        return " OR ".join(f'"{t}"' for t in tokens)

    # ------------------------------------------------------------------ lance
    def _table(self):
        if _TABLE in self._db.list_tables().tables:
            return self._db.open_table(_TABLE)
        return None

    def upsert_document(
        self,
        doc_id: str,
        source_id: str,
        uri: str,
        title: str,
        kind: str,
        chunks: list[str],
        updated_at: str = "",
    ) -> int:
        table = self._table()
        if table is not None:
            table.delete(f'doc_id = "{doc_id}"')
        self._fts_write("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
        if not chunks:
            return 0
        vectors = self._embedder.embed(chunks)
        rows = [
            {
                "id": f"{doc_id}#{i}",
                "doc_id": doc_id,
                "source_id": source_id,
                "uri": uri,
                "title": title or "",
                "kind": kind,
                "updated_at": updated_at,
                "chunk_index": i,
                "text": chunk,
                "vector": vec,
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        if table is None:
            self._db.create_table(_TABLE, data=rows)
        else:
            table.add(rows)
        if self._fts is not None:
            with self._fts_lock:
                self._fts.executemany(
                    "INSERT INTO chunks_fts (title, text, id, doc_id, source_id, uri, kind, updated_at) "
                    "VALUES (:title, :text, :id, :doc_id, :source_id, :uri, :kind, :updated_at)",
                    rows,
                )
                self._fts.commit()
        return len(rows)

    def upsert_document_with_progress(
        self,
        doc_id: str,
        source_id: str,
        uri: str,
        title: str,
        kind: str,
        chunks: list[str],
        updated_at: str = "",
        on_progress: Callable[[int, int], None] | None = None,
        batch_size: int = 16,
    ) -> int:
        """Same contract and result as `upsert_document`, but embeds in small batches and
        calls `on_progress(chunks_done, chunks_total)` after each — for the one caller that
        needs a real, honest number instead of "working…" for however long an unbatched
        `embedder.embed(all_chunks)` call takes (a several-hundred-chunk document on a
        CPU-only embedding model can run minutes; see `ingest/pipeline.py`'s `progress_cb`
        doc comment for why this is a SEPARATE method rather than a parameter added to the
        hot path every connector sync calls). Deliberately duplicates `upsert_document`'s
        small amount of row-building/write logic rather than sharing it, so that method's
        well-exercised path is untouched by this — keep the two in sync if that logic
        changes."""
        table = self._table()
        if table is not None:
            table.delete(f'doc_id = "{doc_id}"')
        self._fts_write("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
        if not chunks:
            return 0
        total = len(chunks)
        vectors: list[list[float]] = []
        for i in range(0, total, batch_size):
            vectors.extend(self._embedder.embed(chunks[i : i + batch_size]))
            if on_progress is not None:
                on_progress(len(vectors), total)
        rows = [
            {
                "id": f"{doc_id}#{i}",
                "doc_id": doc_id,
                "source_id": source_id,
                "uri": uri,
                "title": title or "",
                "kind": kind,
                "updated_at": updated_at,
                "chunk_index": i,
                "text": chunk,
                "vector": vec,
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        if table is None:
            self._db.create_table(_TABLE, data=rows)
        else:
            table.add(rows)
        if self._fts is not None:
            with self._fts_lock:
                self._fts.executemany(
                    "INSERT INTO chunks_fts (title, text, id, doc_id, source_id, uri, kind, updated_at) "
                    "VALUES (:title, :text, :id, :doc_id, :source_id, :uri, :kind, :updated_at)",
                    rows,
                )
                self._fts.commit()
        return len(rows)

    def delete_document(self, doc_id: str) -> None:
        table = self._table()
        if table is not None:
            table.delete(f'doc_id = "{doc_id}"')
        self._fts_write("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))

    def get_document_chunks(self, doc_id: str) -> list[str]:
        """Full ordered chunk texts for one document — chunks are the only place
        the original text lives (the catalog only stores metadata + a hash), so
        anything needing a document's actual content (e.g. repo-doc generation
        reading an existing AGENTS.md back) reconstructs it from here."""
        table = self._table()
        if table is None:
            return []
        try:
            rows = table.to_arrow().select(["doc_id", "chunk_index", "text"]).to_pylist()
        except Exception:
            return []
        matched = [r for r in rows if r["doc_id"] == doc_id]
        matched.sort(key=lambda r: r["chunk_index"])
        return [r["text"] for r in matched]

    def delete_source(self, source_id: str) -> None:
        table = self._table()
        if table is not None:
            table.delete(f'source_id = "{source_id}"')
        self._fts_write("DELETE FROM chunks_fts WHERE source_id = ?", (source_id,))
        # A source purge (clean re-sync / connector cleanup) is a big, explicit delete;
        # reclaim the versions it superseded right away rather than leaving them for the
        # throttled ingest-time pass (a cleanup with no following ingest would never hit it).
        self.compact()

    def reset(self) -> None:
        """Drop every vector + FTS row — the whole corpus. Drops the LanceDB table
        outright (fast, and it's recreated on the next upsert) rather than a per-row
        delete, and clears the FTS sidecar. Used by the global memory reset.

        drop_table removes the table directory entirely — including every superseded
        version — so a system-wide reset reclaims all vector disk on its own (no
        compaction needed). The stale-compaction marker is cleared so the next corpus
        starts its version accounting fresh."""
        if _TABLE in self._db.list_tables().tables:
            self._db.drop_table(_TABLE)
        self._fts_write("DELETE FROM chunks_fts")
        try:
            (self._lance_dir / _COMPACT_MARKER).unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------ compaction
    def compact(self, *, aggressive: bool = False) -> None:
        """Reclaim disk by pruning every superseded LanceDB version (keeping only the
        latest) and compacting small data fragments. Best-effort — any failure leaves
        the data correct, just larger — so it never breaks a sync.

        Safe to call while OTHER sources sync concurrently on the shared table: it
        prunes old versions but keeps `delete_unverified=False`, so files from an
        in-flight append are never removed. `aggressive=True` (delete_unverified) is
        ONLY for a single-process maintenance pass with no concurrent writers."""
        table = self._table()
        if table is None:
            return
        try:
            table.optimize(cleanup_older_than=timedelta(0), delete_unverified=aggressive)
        except Exception:
            pass

    def maybe_compact(self, *, every: int = _COMPACT_EVERY_VERSIONS) -> None:
        """Compact only once `every` table versions have accumulated since the last
        pass. Called at the end of each ingest batch so incremental/normal/clean syncs
        all reclaim disk, while a stream of tiny syncs doesn't pay for a full optimize
        every time. The marker is a small file in the lancedb dir (survives restarts;
        no catalog coupling)."""
        table = self._table()
        if table is None:
            return
        try:
            current = int(table.version)
        except Exception:
            return
        marker = self._lance_dir / _COMPACT_MARKER
        try:
            last = int(marker.read_text())
        except (OSError, ValueError):
            last = 0
        if current - last < every:
            return
        self.compact()
        try:
            marker.write_text(str(int(self._table().version)))
        except (OSError, AttributeError, TypeError):
            pass

    # -------------------------------------------------------------------- ANN
    def ensure_ann_index(self) -> None:
        """Build the approximate (IVF) vector index once the corpus warrants it.
        Below the threshold LanceDB brute-forces — exact and fast at small scale.
        Purely an optimization: any failure leaves search correct, just slower."""
        table = self._table()
        if table is None:
            return
        try:
            if table.count_rows() < self._retrieval.ann_min_rows:
                return
            for idx in table.list_indices():
                cols = getattr(idx, "columns", None) or []
                if "vector" in cols:
                    return  # already indexed
            table.create_index(metric="cosine", vector_column_name="vector")
        except Exception:
            pass

    # ----------------------------------------------------------------- search
    def _dense(self, table, vector, limit: int, id_filter: list[str] | None = None,
               scope: "SearchScope | None" = None) -> list[dict]:
        q = table.search(vector, vector_column_name="vector").metric("cosine")
        clauses = []
        if id_filter:
            clauses.append(f"id IN ({_sql_list(id_filter)})")
        if scope is not None and not scope.is_empty():
            parts = []
            if scope.source_ids:
                parts.append(f"source_id IN ({_sql_list(scope.source_ids)})")
            if scope.doc_ids:
                parts.append(f"doc_id IN ({_sql_list(scope.doc_ids)})")
            clauses.append("(" + " OR ".join(parts) + ")")
        if clauses:
            # prefilter=True: the predicate runs BEFORE the ANN search, so a scoped question
            # searches a smaller space rather than retrieving broadly and discarding.
            q = q.where(" AND ".join(clauses), prefilter=True)
        return q.limit(limit).to_list()

    def search(self, query: str, top_k: int = 8, min_score: float = 0.0,
               scope: "SearchScope | None" = None) -> list[SearchHit]:
        table = self._table()
        if table is None:
            return []
        query = normalize_query(query)
        r = self._retrieval
        vector = self._embedder.embed_query(query)

        hybrid = r.hybrid and self._fts is not None
        fetch = max(top_k * r.candidate_multiplier, top_k) if hybrid else top_k

        # dense leg — rows keyed by chunk id, score = cosine similarity
        by_id: dict[str, dict] = {}
        dense_ids: list[str] = []
        for row in self._dense(table, vector, fetch, scope=scope):
            row["_score"] = 1.0 - float(row.get("_distance", 1.0))
            by_id[row["id"]] = row
            dense_ids.append(row["id"])

        if not hybrid:
            ordered = dense_ids
        else:
            # sparse leg — BM25 over FTS5 (title weighted over body)
            sparse_ids: list[str] = []
            match = self._fts_match(query)
            if match:
                # `source_id`/`doc_id` are UNINDEXED FTS5 columns — not searchable via MATCH,
                # but perfectly usable in WHERE, which is exactly what scoping needs.
                sql = ("SELECT title, text, id, doc_id, source_id, uri, kind, "
                       "bm25(chunks_fts, 2.0, 1.0) AS rank FROM chunks_fts "
                       "WHERE chunks_fts MATCH ?")
                params: list = [match]
                if scope is not None and not scope.is_empty():
                    parts = []
                    if scope.source_ids:
                        parts.append(f"source_id IN ({','.join('?' * len(scope.source_ids))})")
                        params.extend(scope.source_ids)
                    if scope.doc_ids:
                        parts.append(f"doc_id IN ({','.join('?' * len(scope.doc_ids))})")
                        params.extend(scope.doc_ids)
                    sql += " AND (" + " OR ".join(parts) + ")"
                sql += " ORDER BY rank LIMIT ?"
                params.append(fetch)
                with self._fts_lock:
                    fts_rows = self._fts.execute(sql, tuple(params)).fetchall()
                for title, text, id_, doc_id, source_id, uri, kind, _rank in fts_rows:
                    sparse_ids.append(id_)
                    by_id.setdefault(
                        id_,
                        {"id": id_, "doc_id": doc_id, "source_id": source_id, "uri": uri,
                         "title": title, "kind": kind, "text": text},
                    )
            ordered = rrf_fuse([dense_ids, sparse_ids], r.rrf_k)

            # sparse-only candidates still need their cosine score (the grounding
            # gate is always dense) — one targeted, prefiltered vector lookup
            missing = [i for i in ordered if "_score" not in by_id[i]]
            if missing:
                for row in self._dense(table, vector, len(missing), id_filter=missing, scope=scope):
                    by_id[row["id"]]["_score"] = 1.0 - float(row.get("_distance", 1.0))

        # optional second-stage ranking over the fused head
        if self._reranker is not None and len(ordered) > 1:
            head = ordered[: r.rerank_candidates]
            try:
                reordered = self._reranker.rank(query, [by_id[i]["text"] for i in head])
                ordered = [head[j] for j in reordered] + ordered[len(head):]
            except Exception:
                pass  # reranking is best-effort; RRF order stands

        hits = []
        for id_ in ordered:
            row = by_id[id_]
            score = float(row.get("_score", 0.0))
            if score < min_score:
                continue
            hits.append(
                SearchHit(
                    text=row["text"],
                    score=score,
                    doc_id=row["doc_id"],
                    source_id=row["source_id"],
                    uri=row["uri"],
                    title=row.get("title", "") or "",
                    kind=row.get("kind", "doc") or "doc",
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def close(self) -> None:
        if self._fts is not None:
            self._fts.close()
