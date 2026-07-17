"""Cloud backend verification: PostgresCatalog + PgVectorStore against a real Postgres
with pgvector. Skipped unless QJ_TEST_DATABASE_URL points at one (e.g. the
docker-compose.cloud.yml db, or `docker run pgvector/pgvector`). The on-prem SQLite/Lance
path is covered by the rest of the suite; this proves the cloud path matches it.
"""

from __future__ import annotations

import os

import pytest

from quickjoiner.config import Config, RetrievalConfig, SourceConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore

from tests.conftest import FakeEmbedder

DSN = os.environ.get("QJ_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set QJ_TEST_DATABASE_URL to a pgvector Postgres to run cloud backend tests"
)

_TABLES = ("settings", "sources", "documents", "sync_state", "projects",
           "chat_sessions", "users", "auth_tokens", "chunks",
           "entities", "entity_aliases", "edges", "gaps")


def _reset():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS " + ", ".join(_TABLES) + " CASCADE")


@pytest.fixture
def pg():
    _reset()
    from quickjoiner.memory.pg_catalog import PostgresCatalog
    from quickjoiner.memory.pg_store import PgVectorStore

    catalog = PostgresCatalog(DSN)
    store = PgVectorStore(DSN, FakeEmbedder())
    yield catalog, store
    catalog.close()
    store.close()


def test_pg_config_roundtrip(pg):
    catalog, _ = pg
    cfg = Config(org="acme")
    cfg.llm.provider = "ollama"
    cfg.llm.model = "gemma4:cloud"
    cfg.retrieval.min_score = 0.7
    cfg.sources = [
        SourceConfig(name="wiki", type="confluence", options={"base_url": "https://x"},
                     owner="meena", shared=False, sync_interval_minutes=30),
    ]
    catalog.save_config(cfg)

    # A fresh handle (like another container) reads it back from Postgres.
    from quickjoiner.memory.pg_catalog import PostgresCatalog

    other = PostgresCatalog(DSN)
    loaded = other.load_config()
    other.close()
    assert loaded.llm.model == "gemma4:cloud" and loaded.retrieval.min_score == 0.7
    assert len(loaded.sources) == 1
    s = loaded.sources[0]
    assert s.name == "wiki" and s.owner == "meena" and s.shared is False
    assert s.sync_interval_minutes == 30


def test_pg_source_ownership_and_buckets(pg):
    catalog, _ = pg
    catalog.write_source(SourceConfig(name="repo", type="git", options={"url": "u"},
                                      owner="raj", shared=False))
    catalog.upsert_source("notes:user-taught", "user-taught", "notes")  # ingestion bucket

    configs = catalog.list_source_configs()
    assert [s.name for s in configs] == ["repo"]  # buckets excluded
    assert configs[0].owner == "raj" and configs[0].shared is False

    # A sync-path upsert must not reset ownership.
    catalog.upsert_source("git:repo", "repo", "git", {"url": "u2"})
    again = catalog.list_source_configs()[0]
    assert again.owner == "raj" and again.shared is False

    rows = {r["id"]: r for r in catalog.list_sources()}
    assert "git:repo" in rows and "notes:user-taught" in rows  # list_sources shows both


def test_pg_users_tokens(pg):
    catalog, _ = pg
    assert catalog.count_users() == 0
    catalog.create_user("meena", "hash123")
    assert catalog.count_users() == 1 and catalog.get_user("meena")["password_hash"] == "hash123"
    catalog.save_token("tokhash", "meena")
    assert catalog.get_token_user("tokhash") == "meena"
    catalog.delete_token("tokhash")
    assert catalog.get_token_user("tokhash") is None


def test_pg_documents_and_stats(pg):
    catalog, _ = pg
    catalog.upsert_document("d1", "git:repo", "u1", "T1", "code", "hashA", None, 3)
    assert catalog.get_document_hash("d1") == "hashA"
    catalog.upsert_document("d1", "git:repo", "u1", "T1", "code", "hashB", None, 5)  # update
    stats = catalog.stats()
    assert stats["documents"] == 1 and stats["chunks"] == 5


def test_pg_knowledge_graph_roundtrip(pg):
    catalog, _ = pg
    catalog.upsert_document("d9", "git:a", "u9", "Dep map", "doc", "h", None, 1)
    catalog.upsert_entity("repo:a", "a", "repo")
    catalog.upsert_entity("package:x.y.z", "X.Y.Z", "package")
    catalog.add_entity_alias("y z", "package:x.y.z")
    catalog.replace_doc_edges("d9", [("repo:a", "depends_on", "package:x.y.z", "1.0")])

    ent = catalog.resolve_entity("Y Z")  # alias, case-insensitive
    assert ent and ent["id"] == "package:x.y.z"
    rows = catalog.graph_neighbors("package:x.y.z")
    assert rows[0]["src_name"] == "a" and rows[0]["evidence_title"] == "Dep map"

    # graph_path_candidates parity: same single chain as graph_path (plan 06 §A)
    chains = catalog.graph_path_candidates("repo:a", "package:x.y.z")
    assert chains == [catalog.graph_path("repo:a", "package:x.y.z")]

    # entity_evidence parity: adjudication context rows (plan 06 §1.D)
    ev = catalog.entity_evidence("repo:a")
    assert ev == [{"title": "Dep map", "kind": "doc"}]

    # edge_corroboration parity (plan 06 §B)
    assert catalog.edge_corroboration("repo:a", "depends_on", "package:x.y.z") == {
        "doc_count": 1, "source_count": 1}

    catalog.replace_doc_edges("d9", [("repo:a", "depends_on", "package:x.y.z", "2.0")])
    assert catalog.graph_neighbors("package:x.y.z")[0]["detail"] == "2.0"  # replaced, not duped
    catalog.delete_document("d9")
    assert catalog.graph_neighbors("package:x.y.z") == []  # cascade with evidence doc
    assert catalog.graph_snapshot()["edges"] == []


def test_pg_gaps_roundtrip(pg):
    catalog, _ = pg
    catalog.log_gap("how do we deploy with octopus", 0.31,
                    [{"source_id": "files:handbook", "title": "Deploys", "score": 0.31}])
    catalog.log_gap("internal payroll question", 0.1, [], store_query=False)  # hash-only
    rows = catalog.list_gaps("open")
    assert len(rows) == 2
    hashed = next(r for r in rows if r["query"] == "")
    assert hashed["query_hash"]  # hash present even when text withheld
    import json

    kept = next(r for r in rows if r["query"])
    assert json.loads(kept["nearest_json"])[0]["source_id"] == "files:handbook"

    catalog.resolve_gaps([kept["id"]], "connected:octopus")
    assert len(catalog.list_gaps("open")) == 1
    resolved = catalog.list_gaps("resolved")
    assert resolved[0]["resolution"] == "connected:octopus" and resolved[0]["resolved_at"]


def test_pg_sessions(pg):
    catalog, _ = pg
    catalog.save_session("s1", None, "Deploy help", "", "[]", 120)
    assert catalog.get_session("s1")["title"] == "Deploy help"
    rows = catalog.list_sessions()
    assert len(rows) == 1 and rows[0]["est_tokens"] == 120


def test_pg_vector_upsert_search_delete(pg):
    _, store = pg
    assert store.search("anything") == []  # empty
    n = store.upsert_document("doc1", "src", "uri1", "Deploys", "doc",
                              ["Deploys run through Octopus every Friday afternoon."])
    assert n == 1
    store.upsert_document("doc2", "src", "uri2", "Rollback", "doc",
                          ["Roll back by redeploying the previous release."])
    # Query tokens overlap doc1's chunk (octopus/friday) and not doc2's.
    hits = store.search("octopus friday deploys", top_k=5, min_score=0.0)
    assert hits and hits[0].doc_id == "doc1"

    store.delete_document("doc1")
    assert all(h.doc_id != "doc1" for h in store.search("octopus", top_k=5))
    store.delete_source("src")
    assert store.search("octopus", top_k=5) == []


def test_pg_sparse_leg_rescues_exact_token_match(pg):
    """Hybrid parity with the LanceDB store: the full-text leg surfaces exact-token
    matches the dense leg misses, while grounding still gates on the dense cosine.
    (FakeEmbedder sees "err-4711" as one opaque token; Postgres FTS splits it.)"""
    _, store = pg
    store.upsert_document("d1", "src", "u1", "Octopus", "doc",
                          ["the deploy pipeline uses octopus"])
    store.upsert_document("d2", "src", "u2", "Wiki", "doc",
                          ["the confluence wiki has meeting notes"])
    store.upsert_document("d3", "src", "u3", "Errors", "doc",
                          ["err-4711 means the auth cache is stale"])
    hits = store.search("err 4711", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "d3"
    assert hits[0].score < 0.5  # score is the dense cosine, not a fused value
    assert store.search("err 4711", top_k=5, min_score=0.55) == []  # gate stays dense


def test_pg_matches_sqlite_search(pg, tmp_path):
    """Acceptance: identical grounded retrieval on Postgres/pgvector and SQLite/Lance.
    Dense-only — the sparse legs (FTS5/porter vs Postgres/english) tokenize and rank
    differently by design, so fused order is compared per-engine, not across."""
    from quickjoiner.memory.pg_store import PgVectorStore

    dense = RetrievalConfig(hybrid=False)
    pg_catalog = pg[0]
    pg_store = PgVectorStore(DSN, FakeEmbedder(), retrieval=dense)
    sqlite_catalog = Catalog(tmp_path / "ws")
    sqlite_store = KnowledgeStore(tmp_path / "ws", FakeEmbedder(), retrieval=dense)

    docs = [
        Document(uri="w://deploy", title="Deploys", text="Deploys run through Octopus every "
                 "Friday afternoon; roll back by redeploying the previous release.", kind="doc"),
        Document(uri="w://oncall", title="On-call", text="The on-call engineer watches the "
                 "payments dashboard and responds to pages in the deploy-help channel.", kind="doc"),
        Document(uri="w://arch", title="Architecture", text="The platform is a set of services "
                 "behind an API gateway, deployed to the EU-West cluster.", kind="doc"),
    ]
    IngestPipeline(pg_store, pg_catalog).ingest(docs, "wiki")
    IngestPipeline(sqlite_store, sqlite_catalog).ingest(docs, "wiki")

    query = "how do we deploy and roll back"
    pg_hits = pg_store.search(query, top_k=3, min_score=0.0)
    lite_hits = sqlite_store.search(query, top_k=3, min_score=0.0)
    sqlite_catalog.close()
    pg_store.close()

    assert [h.doc_id for h in pg_hits] == [h.doc_id for h in lite_hits]  # same ranking
    assert pg_hits[0].uri == "w://deploy"
    # Scores agree to embedding precision (same FakeEmbedder, same cosine metric).
    for a, b in zip(pg_hits, lite_hits):
        assert abs(a.score - b.score) < 1e-4
