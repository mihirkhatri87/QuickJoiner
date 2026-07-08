from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline


def _docs():
    return [
        Document(uri="repo://a.md", title="a.md", text="# Deploys\nWe deploy with Octopus on Fridays."),
        Document(uri="repo://b.py", title="b.py", text="def hello():\n    return 'world'", kind="code"),
    ]


def test_ingest_is_idempotent(store, catalog):
    pipeline = IngestPipeline(store, catalog)

    first = pipeline.ingest(_docs(), "test:src")
    assert first.added == 2 and first.skipped == 0
    assert first.chunks >= 2

    second = pipeline.ingest(_docs(), "test:src")
    assert second.added == 0 and second.updated == 0 and second.skipped == 2
    assert second.chunks == 0


def test_changed_document_is_updated(store, catalog):
    pipeline = IngestPipeline(store, catalog)
    pipeline.ingest(_docs(), "test:src")

    changed = [Document(uri="repo://a.md", title="a.md", text="# Deploys\nNow we deploy daily.")]
    stats = pipeline.ingest(changed, "test:src")
    assert stats.updated == 1 and stats.added == 0

    hits = store.search("deploy daily", top_k=5)
    assert any("daily" in h.text for h in hits)
    assert not any("Fridays" in h.text for h in hits)  # old chunks replaced


def test_search_respects_min_score(store, catalog):
    pipeline = IngestPipeline(store, catalog)
    pipeline.ingest(_docs(), "test:src")

    exact = store.search("We deploy with Octopus on Fridays.", top_k=5, min_score=0.9)
    assert exact and exact[0].uri == "repo://a.md"

    nothing = store.search("zebra quantum posture", top_k=5, min_score=0.99)
    assert nothing == []


def test_error_in_one_doc_does_not_stop_others(store, catalog, monkeypatch):
    pipeline = IngestPipeline(store, catalog)

    original = store.upsert_document

    def flaky(doc_id, **kwargs):
        if kwargs["uri"] == "repo://a.md":
            raise RuntimeError("boom")
        return original(doc_id=doc_id, **kwargs)

    monkeypatch.setattr(store, "upsert_document", lambda **kw: flaky(**kw))
    stats = pipeline.ingest(_docs(), "test:src")
    assert len(stats.errors) == 1
    assert stats.added == 1
