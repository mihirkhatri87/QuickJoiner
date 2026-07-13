"""Backend selection: on-prem (SQLite + LanceDB files) vs cloud (Postgres + pgvector).

Presence of the DATABASE_URL env var (a `postgres://...` DSN) selects the cloud
backends; otherwise the local file backends are used. The Postgres modules are imported
lazily so the `cloud` extra is only required when it's actually in use.
"""

from __future__ import annotations

import os
from pathlib import Path

from quickjoiner.config import RetrievalConfig
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.embedder import Embedder
from quickjoiner.memory.reranker import create_reranker
from quickjoiner.memory.store import KnowledgeStore


def database_url() -> str | None:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    return dsn or None


def create_catalog(workspace: Path):
    dsn = database_url()
    if dsn:
        from quickjoiner.memory.pg_catalog import PostgresCatalog

        return PostgresCatalog(dsn)
    return Catalog(workspace)


def create_store(workspace: Path, embedder: Embedder, retrieval: RetrievalConfig | None = None):
    reranker = create_reranker(retrieval) if retrieval else None
    dsn = database_url()
    if dsn:
        from quickjoiner.memory.pg_store import PgVectorStore

        return PgVectorStore(dsn, embedder, retrieval=retrieval, reranker=reranker)
    return KnowledgeStore(workspace, embedder, retrieval=retrieval, reranker=reranker)
