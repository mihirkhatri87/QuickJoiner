"""LanceDB disk reclamation: compact() prunes superseded versions, maybe_compact()
self-throttles, delete_source() and the ingest pipeline trigger it, and reset()
clears the throttle marker.

LanceDB is copy-on-write — every delete/add/upsert writes a new table version and
leaves the old one on disk. Nothing pruned them automatically, so a workspace grew
without bound across syncs/clean-resyncs/resets (observed live: ~55k dead versions /
54 GB behind a ~250 MB live corpus). These tests pin the fix.
"""

from __future__ import annotations

from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.memory.store import _COMPACT_MARKER, KnowledgeStore

from tests.conftest import FakeEmbedder


def _versions(store: KnowledgeStore) -> int:
    return len(store._table().list_versions())


def _churn(store: KnowledgeStore, n: int) -> None:
    # Each upsert of an existing doc_id is a delete + an add = two new versions.
    for i in range(n):
        store.upsert_document("d1", "src", "u1", "T", "doc", [f"revision {i}"])


def test_compact_prunes_superseded_versions_but_keeps_data(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _churn(store, 12)
    before = _versions(store)
    assert before > 2  # churn accumulated versions

    store.compact()

    after = _versions(store)
    assert after < before  # old versions reclaimed
    # The live row survives intact — compaction never touches current data.
    hits = store.search("revision 11", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "d1"


def test_maybe_compact_throttles_below_threshold(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _churn(store, 6)
    before = _versions(store)
    store.maybe_compact(every=1000)  # far above the accumulated version count
    assert _versions(store) == before  # nothing pruned
    assert not (store._lance_dir / _COMPACT_MARKER).exists()  # marker only written on a real pass


def test_maybe_compact_fires_and_writes_marker_over_threshold(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _churn(store, 10)
    before = _versions(store)
    store.maybe_compact(every=3)  # threshold well under the accumulated versions
    assert _versions(store) < before
    marker = store._lance_dir / _COMPACT_MARKER
    assert marker.exists() and int(marker.read_text()) == int(store._table().version)


def test_delete_source_compacts_immediately(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    store.upsert_document("a", "keep", "ua", "A", "doc", ["alpha"])
    _churn(store, 8)
    before = _versions(store)
    store.delete_source("src")  # purges the churned doc's source
    assert _versions(store) < before
    # The untouched source is still retrievable.
    hits = store.search("alpha", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "a"


def test_reset_clears_the_compaction_marker(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _churn(store, 4)
    (store._lance_dir / _COMPACT_MARKER).write_text("99")
    store.reset()
    assert not (store._lance_dir / _COMPACT_MARKER).exists()


def test_pipeline_ingest_triggers_throttled_compaction(workspace, catalog, monkeypatch):
    store = KnowledgeStore(workspace, FakeEmbedder())
    calls: list[str] = []
    monkeypatch.setattr(store, "maybe_compact", lambda: calls.append("compact"))
    pipeline = IngestPipeline(store, catalog)
    pipeline.ingest([Document(uri="u1", title="T", text="hello world", kind="doc")], "src")
    assert calls == ["compact"]  # ran once for the batch, after ensure_ann_index
