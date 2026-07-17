from quickjoiner.agent.tools import teach_fact
from quickjoiner.config import RetrievalConfig
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


def test_teach_fact_uri_is_stable_and_timestamp_free(store, catalog):
    # Regression: the taught-note URI must be content-derived, not wall-clock. With
    # contextual chunking (ON here) the URI is embedded in each chunk's breadcrumb, so a
    # volatile timestamp token would perturb the vector run-to-run (flaky retrieval near
    # the grounding gate). Pin the URI to the content hash to prove it carries no clock.
    import hashlib

    pipeline = IngestPipeline(store, catalog, RetrievalConfig())
    fact, topic = "The payments guild owns nautical.", "nautical ownership"
    expected = hashlib.sha256(f"{topic}\n{fact}".encode("utf-8")).hexdigest()[:10]

    teach_fact(catalog, pipeline, fact, topic=topic)
    hit = next(h for h in store.search("payments guild owns nautical", top_k=5, min_score=0.0)
               if h.uri.startswith("note://"))
    assert hit.uri == f"note://nautical-ownership-{expected}"  # content-derived, no unix ts

    # Same fact again is idempotent — same URI, deduped by content (0 added).
    stats = teach_fact(catalog, pipeline, fact, topic=topic)
    assert "0 added" in stats
    note_uris = {h.uri for h in store.search("payments guild owns nautical", top_k=5, min_score=0.0)
                 if h.uri.startswith("note://")}
    assert note_uris == {hit.uri}


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


def test_persist_graph_passes_doc_context_to_resolver(store, catalog):
    """Plan 06 §1.D: the evidence doc's title/kind reach the entity resolver as
    adjudication context, so merges are judged from evidence, not bare names."""
    seen = []

    class SpyResolver:
        def resolve(self, entity_id, name, type_, context=""):
            seen.append((entity_id, context))
            return entity_id, False

    pipeline = IngestPipeline(store, catalog, entity_resolver=SpyResolver())
    doc = Document(
        uri="repo://svc.md", title="Payments service overview", text="x" * 40, kind="doc",
        metadata={"graph": {"entities": [("service:pay", "Payments", "service")],
                            "aliases": [], "edges": []}},
    )
    pipeline.ingest([doc], "test:src")
    assert any(ctx == 'mentioned in "Payments service overview" (doc)'
               for _eid, ctx in seen)
