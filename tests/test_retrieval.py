"""Hybrid retrieval (dense + FTS5 sparse legs), RRF fusion, ingest normalization,
and reranker plumbing.

The exact-token tests exploit a tokenizer asymmetry: FakeEmbedder splits on
whitespace, so "err-4711" is one opaque token the dense leg can't relate to the
query "err 4711" — while FTS5's unicode61 tokenizer splits on the hyphen and
matches both tokens. That makes "sparse rescued what dense missed" fully
deterministic without depending on real embedding geometry.
"""

from __future__ import annotations

from quickjoiner.config import RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.normalize import normalize_query, normalize_text
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.memory.factory import create_store
from quickjoiner.memory.hybrid import rrf_fuse
from quickjoiner.memory.reranker import create_reranker
from quickjoiner.memory.store import KnowledgeStore

from tests.conftest import FakeEmbedder

# ------------------------------------------------------------------ normalize

RAW = "It’s the “deploy” guide — read​ it\r\nnow\n\n\n\nplease  "
CLEAN = 'It\'s the "deploy" guide - read it\nnow\n\nplease'


def test_normalize_text_folds_cosmetic_noise():
    assert normalize_text(RAW) == CLEAN


def test_normalize_text_idempotent():
    assert normalize_text(CLEAN) == CLEAN


def test_normalize_query_folds_and_collapses():
    assert normalize_query(" what’s  the “fix”\nfor this ") == 'what\'s the "fix" for this'


# ------------------------------------------------------------------------ RRF

def test_rrf_fuse_prefers_items_ranked_by_both_legs():
    # b is rank 2 in both legs (2/62) and beats a/c at rank 1 in one leg (1/61)
    assert rrf_fuse([["a", "b"], ["c", "b"]]) == ["b", "a", "c"]


def test_rrf_fuse_ties_break_by_first_appearance():
    assert rrf_fuse([["x"], ["y"]]) == ["x", "y"]
    assert rrf_fuse([["y"], ["x"]]) == ["y", "x"]


# --------------------------------------------------------------------- hybrid

def _seed(store: KnowledgeStore) -> None:
    store.upsert_document("d1", "src", "u1", "Octopus", "doc",
                          ["the deploy pipeline uses octopus"])
    store.upsert_document("d2", "src", "u2", "Wiki", "doc",
                          ["the confluence wiki has meeting notes"])
    store.upsert_document("d3", "src", "u3", "Errors", "doc",
                          ["err-4711 means the auth cache is stale"])


def test_sparse_leg_rescues_exact_token_match(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _seed(store)
    hits = store.search("err 4711", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "d3"
    assert hits[0].score < 0.5  # score is still the (low) dense cosine, not a fused value


def test_min_score_gate_stays_dense_in_hybrid(workspace):
    # The sparse leg surfaces d3, but grounding gates on cosine — so it refuses.
    store = KnowledgeStore(workspace, FakeEmbedder())
    _seed(store)
    assert store.search("err 4711", top_k=5, min_score=0.55) == []


def test_hybrid_off_is_dense_only(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder(), retrieval=RetrievalConfig(hybrid=False))
    _seed(store)
    hits = store.search("auth cache is stale", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "d3"  # dense path still ranks token overlap


def test_upsert_and_delete_keep_fts_in_sync(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder())
    _seed(store)
    store.upsert_document("d3", "src", "u3", "Errors", "doc",
                          ["err-4711 means the auth cache is stale"])  # re-upsert: no dupes
    count = store._fts.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert count == 3
    store.delete_document("d3")
    count = store._fts.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert count == 2
    store.delete_source("src")
    count = store._fts.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert count == 0


def test_fts_backfill_from_existing_lancedb(workspace):
    """Workspaces indexed before the FTS sidecar existed get it rebuilt on open."""
    store = KnowledgeStore(workspace, FakeEmbedder())
    _seed(store)
    store.close()
    (workspace / "fts.db").unlink()

    reopened = KnowledgeStore(workspace, FakeEmbedder())
    count = reopened._fts.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert count == 3
    hits = reopened.search("err 4711", top_k=1, min_score=0.0)
    assert hits and hits[0].doc_id == "d3"  # sparse rescue works again post-backfill


# ------------------------------------------------------------------- reranker

class ReverseReranker:
    def rank(self, query: str, texts: list[str]) -> list[int]:
        return list(range(len(texts)))[::-1]


class ExplodingReranker:
    def rank(self, query: str, texts: list[str]) -> list[int]:
        raise RuntimeError("boom")


def _two_docs(store: KnowledgeStore) -> None:
    store.upsert_document("A", "src", "uA", "A", "doc", ["alpha beta gamma"])
    store.upsert_document("B", "src", "uB", "B", "doc", ["alpha beta delta"])


def test_reranker_reorders_fused_head(workspace):
    plain = KnowledgeStore(workspace / "plain", FakeEmbedder())
    _two_docs(plain)
    assert plain.search("alpha beta gamma", top_k=2, min_score=0.0)[0].doc_id == "A"

    reranked = KnowledgeStore(workspace / "rr", FakeEmbedder(), reranker=ReverseReranker())
    _two_docs(reranked)
    hits = reranked.search("alpha beta gamma", top_k=2, min_score=0.0)
    assert hits[0].doc_id == "B"  # reversed head
    assert hits[0].score < hits[1].score  # scores stay dense cosine, untouched


def test_reranker_failure_degrades_to_rrf_order(workspace):
    store = KnowledgeStore(workspace, FakeEmbedder(), reranker=ExplodingReranker())
    _two_docs(store)
    assert store.search("alpha beta gamma", top_k=2, min_score=0.0)[0].doc_id == "A"


def test_create_reranker_off_unless_configured():
    assert create_reranker(None) is None
    assert create_reranker(RetrievalConfig()) is None  # default "none"


# ------------------------------------------------------------ pipeline wiring

def test_pipeline_normalizes_and_dedupes_cosmetic_variants(workspace, catalog, store):
    pipe = IngestPipeline(store, catalog)
    fancy = Document(uri="w://g", title="Guide",
                     text="It’s the “deploy” guide – use Octopus.", kind="doc")
    assert pipe.ingest([fancy], "wiki").added == 1

    plain = Document(uri="w://g", title="Guide",
                     text='It\'s the "deploy" guide - use Octopus.', kind="doc")
    stats = pipe.ingest([plain], "wiki")
    assert stats.skipped == 1 and stats.chunks == 0  # same content after normalization

    hits = store.search("deploy guide octopus", top_k=1, min_score=0.0)
    assert hits and "“" not in hits[0].text and "It's" in hits[0].text


def test_pipeline_triggers_ann_index_check(workspace, catalog):
    store = KnowledgeStore(workspace, FakeEmbedder())
    calls: list[int] = []
    store.ensure_ann_index = lambda: calls.append(1)
    IngestPipeline(store, catalog).ingest(
        [Document(uri="w://a", title="A", text="alpha beta", kind="doc")], "wiki")
    assert calls  # index maintenance runs after writes


def test_create_store_wires_retrieval_config(tmp_path):
    retrieval = RetrievalConfig(hybrid=False)
    store = create_store(tmp_path / "ws", FakeEmbedder(), retrieval)
    assert store._retrieval is retrieval
    assert store._reranker is None  # reranker off by default
