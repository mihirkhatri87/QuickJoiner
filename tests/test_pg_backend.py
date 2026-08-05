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
           "entities", "entity_aliases", "edges", "gaps", "sync_events")


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


def test_pg_knowledge_scopes_filter_identically_to_sqlite(pg):
    """Knowledge scopes are a SECURITY filter, so "statically reviewed on Postgres" is not
    good enough — this executes the visibility predicate on the real engine, on every read
    shape it takes: `IN (...)` over documents, the `EXISTS` entity reachability subquery,
    and the AND-ed group rendering in `PgVectorStore._scope_sql` (`= ANY(%s)`, which is a
    different spelling from SQLite's and could diverge silently)."""
    from quickjoiner.memory.store import SearchScope

    catalog, store = pg
    catalog.upsert_source("files:handbook", "Handbook", "files")
    catalog.upsert_source("notes:ada", "Ada's notes", "notes")
    catalog.set_source_ownership("notes:ada", owner="ada", shared=False)
    catalog.upsert_document("d-pub", "files:handbook", "u1", "Public", "doc", "h1", None, 1)
    catalog.upsert_document("d-sec", "notes:ada", "u2", "Private", "doc", "h2", None, 1)

    assert set(catalog.visible_source_ids("ada")) == {"files:handbook", "notes:ada"}
    assert catalog.visible_source_ids("bob") == ["files:handbook"]

    catalog.upsert_entity("svc:gateway", "gateway", "service")
    catalog.upsert_entity("person:mole", "Mole", "person")
    catalog.replace_doc_edges("d-sec", [("person:mole", "works_on", "svc:gateway", "")])
    assert catalog.graph_neighbors("svc:gateway") != []
    assert catalog.graph_neighbors("svc:gateway",
                                   visible_source_ids=["files:handbook"]) == []
    # A name is disclosure on its own, so entity autocomplete carries the same filter.
    assert catalog.search_entities("mole") != []
    assert catalog.search_entities("mole", visible_source_ids=["files:handbook"]) == []
    assert catalog.graph_totals(["files:handbook"])["edges"] == 0

    for doc_id, source_id in (("d-pub", "files:handbook"), ("d-sec", "notes:ada")):
        store.upsert_document(doc_id=doc_id, source_id=source_id, uri=f"u://{doc_id}",
                              title="Gateway routing", kind="doc",
                              chunks=["the gateway routes mail through securetide"])
    assert {h.source_id for h in store.search("gateway routes mail", top_k=10)} == \
        {"files:handbook", "notes:ada"}
    as_bob = store.search("gateway routes mail", top_k=10,
                          scope=SearchScope(visible_source_ids=["files:handbook"]))
    assert as_bob and {h.source_id for h in as_bob} == {"files:handbook"}
    # Naming an unreadable source in the picker must not escalate into reading it.
    assert store.search("gateway routes mail", top_k=10,
                        scope=SearchScope(source_ids=["notes:ada"],
                                          visible_source_ids=["files:handbook"])) == []


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


def test_pg_upsert_entity_name_preference_matches_sqlite(pg):
    """The name-preference CASE in upsert_entity's ON CONFLICT is hand-written portable
    SQL, so it needs Postgres parity coverage — the SQLite twin lives in test_graph.py
    (`test_upsert_entity_keeps_incumbent_name_over_worse_cased_variant`)."""
    catalog, _ = pg
    catalog.upsert_entity("repo:nautical", "Nautical", "repo")
    catalog.upsert_entity("repo:nautical", "nautical", "repo")  # LLM-proposed downgrade
    assert catalog.resolve_entity("repo:nautical")["name"] == "Nautical"

    catalog.upsert_entity("repo:stevedore", "stevedore", "repo")
    catalog.upsert_entity("repo:stevedore", "Stevedore", "repo")  # upgrade wins
    assert catalog.resolve_entity("repo:stevedore")["name"] == "Stevedore"

    catalog.upsert_entity("repo:renamed", "Old Name", "repo")
    catalog.upsert_entity("repo:renamed", "New Name", "repo")  # real rename still applies
    assert catalog.resolve_entity("repo:renamed")["name"] == "New Name"


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


def test_pg_whole_graph_snapshot_and_relation_enumeration(pg):
    """The two newest graph reads run the most engine-sensitive SQL in the catalog —
    `graph_snapshot`'s whole-graph branch (nested window functions over a degree
    aggregation, then a dynamically-sized `IN (...)` list) and `graph_relations`'
    optional type constraints. Both are shared `?`-SQL in `_SqlCatalog`, so this is the
    only place the Postgres half of them is executed at all."""
    catalog, _ = pg
    catalog.upsert_document("dt", "web:cat", "u", "Team page", "doc", "h", None, 1)
    for eid, name, typ in [("team:acadia", "Acadia", "team"), ("person:ann", "Ann", "person"),
                           ("person:bo", "Bo", "person"), ("repo:api", "api", "repo")]:
        catalog.upsert_entity(eid, name, typ)
    catalog.replace_doc_edges("dt", [
        ("person:ann", "works_on", "team:acadia", ""),
        ("person:bo", "works_on", "team:acadia", ""),
        ("team:acadia", "owns", "repo:api", ""),
    ])

    snap = catalog.graph_snapshot(limit=400)
    assert {(e["src"], e["rel"], e["dst"]) for e in snap["edges"]} == {
        ("person:ann", "works_on", "team:acadia"),
        ("person:bo", "works_on", "team:acadia"),
        ("team:acadia", "owns", "repo:api"),
    }
    # The denominator ships with the sample on both backends, so a caller can say what it left out.
    assert snap["totals"] == {"edges": 3, "entities": 4} and snap["truncated"] is False
    assert catalog.graph_snapshot(limit=1)["truncated"] is True

    members = catalog.graph_relations("works_on", src_type="person", dst_type="team")
    assert [r["src_name"] for r in members] == ["Ann", "Bo"]
    assert catalog.graph_relations("works_on", src_type="repo") == []  # type constraint bites


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


def test_pg_reset_knowledge(pg):
    """The global memory reset runs the same neutral SQL on Postgres."""
    catalog, _ = pg
    catalog.write_source(SourceConfig(name="repo", type="git", options={"url": "u"}))
    catalog.upsert_source("notes:taught", "taught", "notes")
    catalog.upsert_document("d1", "git:repo", "u::a", "A", "code", "h1", "2026-07-01", 2)
    catalog.upsert_entity("repo:repo", "repo", "repo", "git:repo")
    catalog.replace_doc_edges("d1", [("repo:repo", "defines", "symbol:foo", "")])
    catalog.set_sync_state("git:repo", "since", "2026-07-01")

    counts = catalog.reset_knowledge()
    assert counts["documents"] == 1 and counts["edges"] == 1
    assert catalog.stats()["documents"] == 0
    assert catalog.graph_snapshot()["edges"] == []
    assert catalog.get_sync_state("git:repo") == {}
    assert [s.name for s in catalog.list_source_configs()] == ["repo"]  # connector kept


def test_pg_sync_events_roundtrip(pg):
    """The 24h activity history is written on both backends by the same neutral SQL —
    including the upsert that turns the 'running' row into its final state."""
    catalog, _ = pg
    catalog.record_sync_event("sync-1-abc", "handbook", "running", False, "2026-07-20T10:00:00+00:00")
    catalog.record_sync_event("sync-1-abc", "handbook", "done", False, "2026-07-20T10:00:00+00:00",
                              ended_at="2026-07-20T10:04:00+00:00",
                              stats={"added": 3, "updated": 0, "skipped": 1, "chunks": 9, "errors": 0})
    # Finished, so prunable. An old run with no `ended_at` is a *paused* one and is kept
    # however old it gets, so it can still be resumed after a restart — the SQLite suite
    # pins that separately in `test_prune_never_drops_an_unfinished_paused_run`.
    catalog.record_sync_event("sync-0-old", "handbook", "done", True, "2026-07-01T09:00:00+00:00",
                              ended_at="2026-07-01T09:05:00+00:00")

    rows = catalog.list_sync_events("2026-07-20T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["state"] == "done"  # upserted in place, older run filtered out
    import json

    assert json.loads(rows[0]["stats_json"])["added"] == 3
    assert catalog.prune_sync_events("2026-07-20T00:00:00+00:00") == 1  # drops the July 1 run
    assert len(catalog.list_sync_events("2000-01-01T00:00:00+00:00")) == 1


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
