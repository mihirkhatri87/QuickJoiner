"""Catalog primitives that aren't exercised through a higher-level suite.

The Postgres twins of these live in test_pg_backend.py (env-gated); the SQL itself is
shared, in the backend-neutral `_SqlCatalog`.
"""

from __future__ import annotations

import json

from pydantic import Field

from quickjoiner.config import (
    SUPERSEDED_DEFAULTS,
    Config,
    GraphConfig,
    RetrievalConfig,
    SourceConfig,
    reconcile_superseded_defaults,
)


def test_reset_knowledge_wipes_ingested_but_keeps_connectors(catalog):
    """The global memory reset: everything ingested/derived goes, connector configs stay."""
    # A configured connector + an ingestion bucket, each with a document + graph.
    catalog.write_source(SourceConfig(name="repo", type="git", options={"url": "u"}))
    catalog.upsert_source("notes:taught", "taught", "notes")  # bucket (configured=0)
    catalog.upsert_document("d1", "git:repo", "u::a", "A", "code", "h1", "2026-07-01", 2)
    catalog.upsert_document("d2", "notes:taught", "note://x", "N", "note", "h2", "2026-07-01", 1)
    catalog.upsert_entity("repo:repo", "repo", "repo", "git:repo")
    catalog.replace_doc_edges("d1", [("repo:repo", "defines", "symbol:foo", "")])
    catalog.mark_graph_pending("d2", "notes:taught")
    catalog.set_sync_state("git:repo", "since", "2026-07-01T00:00:00Z")
    catalog.log_gap("how do we deploy", 0.2, [])

    assert catalog.stats()["documents"] == 2

    counts = catalog.reset_knowledge()
    assert counts["documents"] == 2 and counts["edges"] == 1

    # Everything ingested/derived is gone.
    assert catalog.stats()["documents"] == 0
    assert catalog.graph_snapshot()["edges"] == [] and catalog.graph_snapshot()["nodes"] == []
    assert catalog.count_graph_pending() == 0
    assert catalog.get_sync_state("git:repo") == {}  # watermark cleared → next sync full
    assert catalog.list_gaps("open") == []

    # The configured connector survives (listed, re-syncable); the bucket is gone.
    configs = catalog.list_source_configs()
    assert [s.name for s in configs] == ["repo"]


def test_reset_knowledge_can_keep_gaps(catalog):
    catalog.log_gap("q", 0.1, [])
    catalog.reset_knowledge(include_gaps=False)
    assert len(catalog.list_gaps("open")) == 1


def test_sync_events_upsert_list_and_prune(catalog):
    """The 24h activity history behind /api/notifications: one row per run, updated in
    place when the run ends, windowed by start time, pruned by age."""
    catalog.record_sync_event("sync-1-abc", "handbook", "running", False, "2026-07-20T10:00:00+00:00")
    catalog.record_sync_event(
        "sync-1-abc", "handbook", "done", False, "2026-07-20T10:00:00+00:00",
        ended_at="2026-07-20T10:04:00+00:00",
        stats={"added": 3, "updated": 0, "skipped": 1, "chunks": 9, "errors": 0},
    )
    catalog.record_sync_event("sync-2-def", "tickets", "error", True, "2026-07-20T11:00:00+00:00",
                              ended_at="2026-07-20T11:00:30+00:00", error="401 from the broker")
    catalog.record_sync_event("sync-0-old", "handbook", "done", False, "2026-07-01T09:00:00+00:00",
                              ended_at="2026-07-01T09:05:00+00:00")

    recent = catalog.list_sync_events("2026-07-20T00:00:00+00:00")
    assert [r["id"] for r in recent] == ["sync-2-def", "sync-1-abc"]  # newest first

    done = next(r for r in recent if r["id"] == "sync-1-abc")
    assert done["state"] == "done"  # the finished row replaced the running one
    assert json.loads(done["stats_json"])["added"] == 3
    assert done["ended_at"]

    failed = next(r for r in recent if r["id"] == "sync-2-def")
    assert failed["error"] == "401 from the broker" and failed["clean"] == 1

    assert catalog.list_sync_events("2026-07-20T00:00:00+00:00", limit=1) == recent[:1]

    assert catalog.prune_sync_events("2026-07-20T00:00:00+00:00") == 1  # only the July 1 run
    assert len(catalog.list_sync_events("2000-01-01T00:00:00+00:00")) == 2


def test_postgres_placeholder_translation_escapes_percent_signs():
    """The `?` -> `%s` swap must escape any literal `%` in the statement first.

    psycopg reads a bare `%` anywhere in the SQL — a `--` comment included — as the start
    of a placeholder and raises `incomplete placeholder`, a failure the SQLite adapter can
    never reproduce. A `-- 57% degree-1 nodes` comment in `graph_snapshot` broke the
    whole-graph view on Postgres only, and the Postgres suite is env-gated, so this pure
    test is what catches the next one without Docker. `_pg` is a staticmethod and the
    module imports psycopg lazily, so this runs anywhere.
    """
    from quickjoiner.memory.pg_catalog import PostgresCatalog

    assert PostgresCatalog._pg("SELECT ? -- 57% here") == "SELECT %s -- 57%% here"
    # The markers this function writes are not themselves re-escaped.
    assert PostgresCatalog._pg("WHERE a = ? AND b = ?") == "WHERE a = %s AND b = %s"
    # Escaping at this choke point is what covers the inline query strings too — they live
    # in method bodies and cannot be enumerated, which is why the guard belongs here and
    # not in a scan over the SQL.


def test_prune_never_drops_an_unfinished_paused_run(catalog):
    """A deliberately-paused sync (ended_at IS NULL) must survive the retention window so it
    can be resumed after a restart, however long the laptop was closed. list_unfinished_syncs
    is what revive_paused reads to find it."""
    catalog.record_sync_event("sync-paused-old", "handbook", "paused", False,
                              "2020-01-01T00:00:00+00:00")  # no ended_at => unfinished
    catalog.record_sync_event("sync-done-old", "tickets", "done", False,
                              "2020-01-01T00:00:00+00:00", ended_at="2020-01-01T00:05:00+00:00")

    pruned = catalog.prune_sync_events("2026-07-20T00:00:00+00:00")
    assert pruned == 1  # the finished old run went; the paused one stayed

    unfinished = catalog.list_unfinished_syncs()
    assert [r["id"] for r in unfinished] == ["sync-paused-old"]
    assert unfinished[0]["state"] == "paused"


def test_document_metadata_json_round_trips_and_defaults_empty(catalog):
    # A document upserted with no metadata_json (every non-ADO connector) reads back as an
    # empty dict, not null — callers do metadata_json.get(...) without a None check.
    catalog.upsert_document("d1", "git:repo", "u::a", "A", "code", "h1", "2026-07-01", 2)
    row = catalog.documents_for_source("git:repo")[0]
    assert json.loads(row["metadata_json"]) == {}

    meta = {"work_item_type": "Story", "state": "Active", "team": "Payments", "parent_id": 12}
    catalog.upsert_document("d2", "azure_devops:tfs", "u::b", "B", "ticket", "h2", "2026-07-01", 1,
                            metadata_json=json.dumps(meta))
    row2 = next(r for r in catalog.documents_for_source("azure_devops:tfs") if r["doc_id"] == "d2")
    assert json.loads(row2["metadata_json"]) == meta


def test_update_document_metadata_is_a_standalone_backfill(catalog):
    # The path an unchanged document's team/sprint/state refresh through — no hash/chunk
    # change, just the metadata blob, matching the graph-version staleness refresh it rides
    # alongside (see pipeline.py's stale_graph branch).
    catalog.upsert_document("d1", "azure_devops:tfs", "u::a", "A", "ticket", "h1", "2026-07-01", 2,
                            metadata_json=json.dumps({"state": "Active"}))
    catalog.update_document_metadata("d1", json.dumps({"state": "Closed", "team": "Payments"}))
    row = catalog.documents_for_source("azure_devops:tfs")[0]
    assert json.loads(row["metadata_json"]) == {"state": "Closed", "team": "Payments"}
    assert row["content_hash"] == "h1"  # untouched — this is a metadata-only update


# --- shipped config defaults reaching an existing workspace ---------------------------
# Regression cover for the defect found in live testing (2026-07-31): save_config dumped
# EVERY field, so the first save pinned every value forever and plan 05's min_score
# retune never reached the only real corpus it was calibrated against.


def test_config_is_persisted_sparsely_so_shipped_defaults_stay_live(catalog):
    """Only real customisations are stored; untouched fields follow config.py."""
    config = catalog.load_config()
    config.retrieval.top_k = 11  # a genuine customisation
    catalog.save_config(config)

    blob = json.loads(catalog.get_setting("config"))
    assert blob["retrieval"] == {"top_k": 11}  # min_score & co. are NOT materialised
    assert "chat" not in blob and "llm" not in blob

    # An untouched field reads back as whatever the code currently ships...
    assert catalog.load_config().retrieval.min_score == RetrievalConfig().min_score
    assert catalog.load_config().retrieval.top_k == 11  # ...and the customisation survives

    # ...so a retune shipped in config.py DOES reach this already-saved workspace, which
    # is the whole point. Modelled as a subclass because pydantic bakes field defaults
    # into the compiled schema, so monkeypatching one has no effect on validation.
    class _Retuned(RetrievalConfig):
        min_score: float = 0.9

    class _RetunedConfig(Config):
        retrieval: _Retuned = Field(default_factory=_Retuned)

    retuned = _RetunedConfig.model_validate(blob)
    assert retuned.retrieval.min_score == 0.9  # untouched field follows the new default
    assert retuned.retrieval.top_k == 11  # customisation still wins over it


def test_a_pinned_superseded_default_is_adopted_once_and_customisations_are_kept(catalog):
    """The migration for blobs written before sparse persistence.

    Mirrors the live workspace exactly: min_score pinned at the superseded 0.55, and a
    deliberate triple_workers=8 that matches no shipped default.
    """
    catalog.load_config()  # create the workspace's settings row
    dense = {
        "org": "appriver",
        "retrieval": {"top_k": 8, "min_score": 0.55, "reranker": "fastembed"},
        "graph": {"extract_triples": True, "triple_workers": 8},
    }
    catalog.set_setting("config", json.dumps(dense))
    catalog.set_setting("config_defaults_epoch", "0")  # as if written by the old code

    config = catalog.load_config()
    assert config.retrieval.min_score == RetrievalConfig().min_score  # 0.55 -> shipped
    assert config.graph.triple_workers == 8  # deliberate value, not a superseded default
    assert config.retrieval.top_k == 8 and config.graph.extract_triples is True

    stored = json.loads(catalog.get_setting("config"))
    assert "min_score" not in stored["retrieval"]  # pruned, so it tracks the code now
    assert stored["graph"]["triple_workers"] == 8

    # Runs once: a later deliberate 0.55 is a real choice and must NOT be re-adopted.
    config.retrieval.min_score = 0.55
    catalog.save_config(config)
    assert catalog.load_config().retrieval.min_score == 0.55


def test_reconcile_superseded_defaults_is_pure_and_reports_what_moved():
    blob = {"retrieval": {"min_score": 0.55}, "graph": {"triple_workers": 4}}
    pruned, adopted = reconcile_superseded_defaults(blob)

    assert blob == {"retrieval": {"min_score": 0.55}, "graph": {"triple_workers": 4}}  # unmutated
    assert pruned == {}  # both sections emptied and dropped
    assert sorted(adopted) == [
        ("graph.triple_workers", 4, GraphConfig().triple_workers),
        ("retrieval.min_score", 0.55, RetrievalConfig().min_score),
    ]
    # A value matching no superseded default is untouched, whatever it is.
    assert reconcile_superseded_defaults({"retrieval": {"min_score": 0.71}}) == (
        {"retrieval": {"min_score": 0.71}}, [],
    )


def test_every_superseded_default_names_a_real_config_field():
    """Lockstep: a renamed/removed field must not leave a dead migration entry behind."""
    fresh = Config()
    for path, superseded in SUPERSEDED_DEFAULTS.items():
        group, _, field = path.partition(".")
        section = getattr(fresh, group)
        assert field in type(section).model_fields, f"{path} names no live config field"
        current = getattr(section, field)
        assert current not in superseded, f"{path}: current default is listed as superseded"
