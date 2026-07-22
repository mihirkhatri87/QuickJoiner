"""Catalog primitives that aren't exercised through a higher-level suite.

The Postgres twins of these live in test_pg_backend.py (env-gated); the SQL itself is
shared, in the backend-neutral `_SqlCatalog`.
"""

from __future__ import annotations

import json

from quickjoiner.config import SourceConfig


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
