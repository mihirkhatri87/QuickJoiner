"""pgvector-backed knowledge store (cloud mode). Mirrors KnowledgeStore's surface
(upsert_document / delete_document / delete_source / search) against a shared Postgres
`chunks` table with an HNSW cosine index. Vectors are sent as pgvector text literals
(`'[...]'::vector`) so no extra Python adapter is needed — only the DB extension.

Hybrid retrieval matches the LanceDB store: a dense cosine leg plus a sparse
full-text leg (generated tsvector column + GIN index, tokens OR-ed like the
FTS5 sidecar) fused with RRF; an optional cross-encoder reorders the fused head.
Grounding contract is identical: SearchHit.score is ALWAYS the dense cosine
score and retrieval.min_score gates on it — sparse-only candidates get their
cosine computed via a targeted lookup before the gate.
"""

from __future__ import annotations

import re

from quickjoiner.config import RetrievalConfig
from quickjoiner.ingest.normalize import normalize_query
from quickjoiner.memory.embedder import Embedder
from quickjoiner.memory.hybrid import rrf_fuse
from quickjoiner.memory.store import NULL_TRACE, NullTrace, SearchHit

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class PgVectorStore:
    def __init__(
        self,
        dsn: str,
        embedder: Embedder,
        retrieval: RetrievalConfig | None = None,
        reranker=None,
        min_size: int = 1,
        max_size: int = 10,
    ):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Cloud mode needs the 'cloud' extra: pip install -e \".[cloud]\""
            ) from exc

        self._embedder = embedder
        self._dim = embedder.dim  # fixes the vector column width for this workspace
        self._retrieval = retrieval or RetrievalConfig()
        self._reranker = reranker
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        with self._pool.connection() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    uri TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'doc', updated_at TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL, text TEXT NOT NULL, vector vector({self._dim}),
                    tsv tsvector GENERATED ALWAYS AS
                        (to_tsvector('english', title || ' ' || text)) STORED)"""
            )
            # Upgrade path: tables created before the sparse leg existed.
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || text)) STORED"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks "
                "USING hnsw (vector vector_cosine_ops)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv)")

    @property
    def embedder(self):
        return self._embedder

    def set_retrieval(self, retrieval, reranker=None) -> None:
        """Adopt changed query-side settings without reopening the connection pool —
        mirrors KnowledgeStore.set_retrieval (see AppContext.apply_config)."""
        self._retrieval = retrieval or RetrievalConfig()
        self._reranker = reranker

    @staticmethod
    def _vec(values) -> str:
        return "[" + ",".join(repr(float(x)) for x in values) + "]"

    @staticmethod
    def _tsquery(query: str) -> str:
        """OR-ed quoted tokens, same semantics as the FTS5 sidecar's MATCH:
        any token may hit; ts_rank rewards docs matching more/rarer ones."""
        tokens = _TOKEN.findall(query)[:32]
        return " | ".join(f"'{t}'" for t in tokens)

    def upsert_document(self, doc_id, source_id, uri, title, kind, chunks, updated_at="") -> int:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            if not chunks:
                return 0
            vectors = self._embedder.embed(chunks)
            with conn.cursor() as cur:
                for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    cur.execute(
                        "INSERT INTO chunks (id, doc_id, source_id, uri, title, kind, "
                        "updated_at, chunk_index, text, vector) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                        (f"{doc_id}#{i}", doc_id, source_id, uri, title or "", kind,
                         updated_at, i, chunk, self._vec(vec)),
                    )
            return len(chunks)

    def delete_document(self, doc_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))

    def move_document(self, doc_id: str, source_id: str) -> None:
        """Re-home an indexed document to another source in place — see the LanceDB
        twin in `store.py` for why promotion is a column rewrite and not a re-ingest."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE chunks SET source_id = %s WHERE doc_id = %s",
                         (source_id, doc_id))

    def get_document_chunks(self, doc_id: str) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE doc_id = %s ORDER BY chunk_index", (doc_id,)
            ).fetchall()
        return [row["text"] for row in rows]

    def get_documents_chunks(self, doc_ids: list[str]) -> dict[str, list[str]]:
        """Ordered chunk texts for many documents in one round trip (see the LanceDB
        store's note — the graph-pending drain reads thousands)."""
        if not doc_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT doc_id, text FROM chunks WHERE doc_id = ANY(%s) "
                "ORDER BY doc_id, chunk_index",
                (list(dict.fromkeys(doc_ids)),),
            ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["doc_id"], []).append(row["text"])
        return out

    def delete_source(self, source_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE source_id = %s", (source_id,))

    def reset(self) -> None:
        """Drop every vector row — the whole corpus (global memory reset)."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks")

    @staticmethod
    def _scope_sql(scope) -> tuple[str, list]:
        """`(sql_fragment, params)` restricting a query to a scope — the pgvector twin of
        KnowledgeStore's predicate, so both backends filter identically (and before ranking,
        not after)."""
        if scope is None or scope.is_empty():
            return "", []
        groups, params = [], []
        for group in scope.predicate_groups():
            terms = []
            for col, vals in group:
                # An empty list is unsatisfiable, and `= ANY('{}')` is already exactly that
                # in Postgres — but spell it out so the intent survives a later edit.
                if not vals:
                    terms.append("FALSE")
                    continue
                terms.append(f"{col} = ANY(%s)")
                params.append(list(vals))
            groups.append("(" + (" OR ".join(terms) or "FALSE") + ")")
        return " AND ".join(groups), params

    def search(self, query: str, top_k: int = 8, min_score: float = 0.0,
               scope=None, trace: "NullTrace | None" = None) -> list[SearchHit]:
        trace = trace or NULL_TRACE
        query = normalize_query(query)
        r = self._retrieval
        with trace.stage("embed_query"):
            vector = self._vec(self._embedder.embed_query(query))
        hybrid = r.hybrid
        fetch = max(top_k * r.candidate_multiplier, top_k) if hybrid else top_k
        scope_sql, scope_params = self._scope_sql(scope)

        with self._pool.connection() as conn:
            # dense leg — rows keyed by chunk id, score = cosine similarity
            with trace.stage("dense"):
                rows = conn.execute(
                    "SELECT id, text, doc_id, source_id, uri, title, kind, "
                    "1 - (vector <=> %s::vector) AS score FROM chunks "
                    + (f"WHERE {scope_sql} " if scope_sql else "")
                    + "ORDER BY vector <=> %s::vector LIMIT %s",
                    (vector, *scope_params, vector, fetch),
                ).fetchall()
            by_id = {row["id"]: dict(row) for row in rows}
            dense_ids = [row["id"] for row in rows]
            trace.count("dense_candidates", len(dense_ids))

            if not hybrid:
                ordered = dense_ids
            else:
                # sparse leg — full-text rank over the generated tsvector
                sparse_ids: list[str] = []
                tsq = self._tsquery(query)
                if tsq:
                    with trace.stage("sparse"):
                        sparse_rows = conn.execute(
                            "SELECT id, text, doc_id, source_id, uri, title, kind, "
                            "ts_rank(tsv, to_tsquery('english', %s)) AS rank "
                            "FROM chunks WHERE tsv @@ to_tsquery('english', %s) "
                            + (f"AND {scope_sql} " if scope_sql else "")
                            + "ORDER BY rank DESC LIMIT %s",
                            (tsq, tsq, *scope_params, fetch),
                        ).fetchall()
                        for row in sparse_rows:
                            sparse_ids.append(row["id"])
                            by_id.setdefault(row["id"], dict(row))
                trace.count("sparse_candidates", len(sparse_ids))
                with trace.stage("fuse"):
                    ordered = rrf_fuse([dense_ids, sparse_ids], r.rrf_k)

                # sparse-only candidates still need their cosine score (the
                # grounding gate is always dense) — one targeted lookup
                missing = [i for i in ordered if "score" not in by_id[i]]
                trace.count("sparse_only", len(missing))
                if missing:
                    with trace.stage("sparse_rescore"):
                        for row in conn.execute(
                            "SELECT id, 1 - (vector <=> %s::vector) AS score "
                            "FROM chunks WHERE id = ANY(%s)",
                            (vector, missing),
                        ).fetchall():
                            by_id[row["id"]]["score"] = row["score"]

        # optional second-stage ranking over the fused head
        trace.count("fused", len(ordered))
        if self._reranker is not None and len(ordered) > 1:
            head = ordered[: r.rerank_candidates]
            trace.count("reranked", len(head))
            try:
                with trace.stage("rerank"):
                    reordered = self._reranker.rank(query, [by_id[i]["text"] for i in head])
                ordered = [head[j] for j in reordered] + ordered[len(head):]
            except Exception:
                pass  # reranking is best-effort; RRF order stands

        hits = []
        with trace.stage("gate"):
            for id_ in ordered:
                row = by_id[id_]
                score = float(row.get("score", 0.0))
                if score < min_score:
                    continue
                hits.append(SearchHit(
                    text=row["text"], score=score, doc_id=row["doc_id"],
                    source_id=row["source_id"], uri=row["uri"],
                    title=row["title"] or "", kind=row["kind"] or "doc",
                ))
                if len(hits) >= top_k:
                    break
        trace.count("hits", len(hits))
        return hits

    def close(self) -> None:
        self._pool.close()
