"""Hybrid retrieval (dense + FTS5 sparse legs), RRF fusion, ingest normalization,
and reranker plumbing.

The exact-token tests exploit a tokenizer asymmetry: FakeEmbedder splits on
whitespace, so "err-4711" is one opaque token the dense leg can't relate to the
query "err 4711" — while FTS5's unicode61 tokenizer splits on the hyphen and
matches both tokens. That makes "sparse rescued what dense missed" fully
deterministic without depending on real embedding geometry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quickjoiner.config import RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.normalize import normalize_query, normalize_text
from quickjoiner.ingest.pipeline import IngestPipeline, breadcrumb
from quickjoiner.memory.factory import create_store
from quickjoiner.memory.hybrid import rrf_fuse
from quickjoiner.memory.reranker import (
    BASE_RERANK_MODEL,
    DEFAULT_RERANK_MODEL,
    CrossEncoderReranker,
    _register_quantized,
    create_reranker,
)
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


def test_reranker_default_on_but_disableable():
    # On by default (built lazily on first use, so no download here); "none" disables it.
    assert RetrievalConfig().reranker == "fastembed"
    assert create_reranker(None) is None
    assert create_reranker(RetrievalConfig(reranker="none")) is None


def test_default_reranker_is_the_quantized_build_with_an_fp32_fallback():
    """The default cross-encoder is the INT8 build (S4a, 2026-08-11): measured on the live
    corpus it is 28.6% faster on the rerank stage (paired, order-alternated — the naive
    fp32-first framings said 38-41% and were measuring the machine warming up) with recall,
    grounded recall, MRR, hop coverage and refusal accuracy all identical to fp32.
    fastembed's catalogue does not carry it, so we register the same HF repo pointed at its
    quantized ONNX.

    The fp32 fallback is the part that matters: an existing install already has the 91MB
    fp32 model cached, so an upgrade that could ONLY reach the new file would turn an
    offline machine's working reranker into a silent degrade-to-RRF — a quality regression
    caused by upgrading. An explicitly pinned model is never substituted this way."""
    assert DEFAULT_RERANK_MODEL.startswith(BASE_RERANK_MODEL)
    assert DEFAULT_RERANK_MODEL != BASE_RERANK_MODEL

    assert CrossEncoderReranker()._candidates() == [DEFAULT_RERANK_MODEL, BASE_RERANK_MODEL]
    pinned = CrossEncoderReranker(BASE_RERANK_MODEL)
    assert pinned._candidates() == [BASE_RERANK_MODEL]  # a choice is a choice


def test_reranker_falls_back_to_fp32_when_the_quantized_build_is_unreachable(monkeypatch):
    """Offline/rate-limited first load of the INT8 file must cost the quantization, not
    the whole reranking stage."""
    tried: list[str] = []

    def only_fp32_builds(self, name):
        tried.append(name)
        if name != BASE_RERANK_MODEL:
            raise RuntimeError("404 from the hub")
        return ReverseReranker()  # stand-in for a loaded encoder

    monkeypatch.setattr(CrossEncoderReranker, "_build", only_fp32_builds)
    rr = CrossEncoderReranker()
    rr._ensure()
    assert tried == [DEFAULT_RERANK_MODEL, BASE_RERANK_MODEL]
    assert rr._encoder is not CrossEncoderReranker._FAILED  # still reranking
    assert rr._model_name == BASE_RERANK_MODEL              # reports what actually loaded

    # ...and when nothing loads at all, it still degrades quietly to RRF order.
    monkeypatch.setattr(CrossEncoderReranker, "_build",
                        lambda self, name: (_ for _ in ()).throw(RuntimeError("no network")))
    dead = CrossEncoderReranker()
    dead._ensure()
    assert dead._encoder is CrossEncoderReranker._FAILED
    assert dead.rank("q", ["a", "b"]) == [0, 1]  # input order, no exception


def test_quantized_model_registration_is_idempotent():
    """fastembed raises on re-registering a name, and a reranker is rebuilt on every
    settings change (AppContext.apply_config) — so the second call must be a no-op."""
    pytest.importorskip("fastembed")
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    _register_quantized()
    _register_quantized()  # would raise ValueError if unguarded
    names = [m["model"] for m in TextCrossEncoder.list_supported_models()]
    assert names.count(DEFAULT_RERANK_MODEL) == 1


class GoldReranker:
    """Promotes whichever candidate contains 'gold', and records how many it was shown."""

    def __init__(self) -> None:
        self.depths: list[int] = []

    def rank(self, query: str, texts: list[str]) -> list[int]:
        self.depths.append(len(texts))
        return sorted(range(len(texts)), key=lambda i: 0 if "gold" in texts[i] else 1)


def _buried_gold(store: KnowledgeStore, n: int = 20) -> None:
    for i in range(n):
        store.upsert_document(f"D{i}", "src", f"u{i}", f"D{i}", "doc", [f"alpha beta filler{i}"])
    store.upsert_document("GOLD", "src", "ugold", "GOLD", "doc", ["alpha gold nugget"])


def test_rerank_depth_bounds_which_candidates_can_be_promoted(workspace):
    """`rerank_candidates` is a recall knob, not only a cost dial: a candidate the fused
    order buried BELOW it is never shown to the cross-encoder, so no amount of relevance
    can bring it back. That is the floor under the default — measured on the live corpus
    (2026-08-10), the deepest expected source in the eval set sits at fused rank 12, and
    every depth below that lost the case outright while every depth at or above it scored
    identically. Pinned here as the mechanism rather than as a number."""
    plain = KnowledgeStore(workspace / "plain", FakeEmbedder())
    _buried_gold(plain)
    order = [h.doc_id for h in plain.search("alpha beta", top_k=64, min_score=0.0)]
    depth_needed = order.index("GOLD") + 1
    assert depth_needed > 1, "the fixture must actually bury GOLD for this to test anything"

    def _search(depth: int) -> tuple[str, GoldReranker]:
        rr = GoldReranker()
        store = KnowledgeStore(
            workspace / f"rr{depth}", FakeEmbedder(),
            retrieval=RetrievalConfig(rerank_candidates=depth), reranker=rr,
        )
        _buried_gold(store)
        return store.search("alpha beta", top_k=64, min_score=0.0)[0].doc_id, rr

    top_shallow, shallow = _search(depth_needed - 1)
    assert top_shallow != "GOLD"                      # out of reach: never scored
    assert shallow.depths and max(shallow.depths) == depth_needed - 1

    top_deep, deep = _search(depth_needed)
    assert top_deep == "GOLD"                         # in reach: promoted to the top
    assert max(deep.depths) == depth_needed


def test_default_rerank_depth_stays_above_the_measured_floor():
    """The default was cut 24 -> 16 on measured evidence (S4): quality was flat from depth
    12 to 32 on the live corpus, so two thirds of the cross-encoder's work bought nothing.
    16 rather than 12 is deliberate margin — 12 is where a real expected source sat, and
    sitting exactly on a measured cliff is how a cheaper default turns into lost recall on
    the next corpus. A future cut below that floor needs its own measurement, not a guess."""
    assert RetrievalConfig().rerank_candidates >= 12


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
    retrieval = RetrievalConfig(hybrid=False, reranker="none")
    store = create_store(tmp_path / "ws", FakeEmbedder(), retrieval)
    assert store._retrieval is retrieval
    assert store._reranker is None  # explicitly disabled -> no cross-encoder built


# --------------------------------------------------------------- IVF_PQ scoring
# Regression for the plan-05 finding (2026-07-28): LanceDB's default ANN index is
# IVF_PQ (product-quantized), which distorts `_dense`'s cosine SCORE by 0.25-0.45 —
# not just recall — because `min_score` gates on that same score. No prior test
# ever crossed `ann_min_rows` with a real index, so nothing caught it. LanceDB
# needs >=256 rows to train PQ at all, hence the bulk of rows below.

_ANN_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
              "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
              "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega", "zero",
              "one", "two", "three", "four", "five"]


def _bulk_chunks(n: int, seed: int = 0) -> list[str]:
    import random
    rng = random.Random(seed)
    return [" ".join(rng.choices(_ANN_WORDS, k=6)) for _ in range(n)]


def test_ann_refine_factor_restores_exact_scores_above_min_rows(workspace):
    chunks = _bulk_chunks(400)
    query = "alpha beta gamma delta epsilon zeta"

    # Exact (brute-force, no index): the ground truth. ann_min_rows is set above
    # the row count so no index gets built.
    exact_store = KnowledgeStore(workspace / "exact", FakeEmbedder(),
                                  retrieval=RetrievalConfig(ann_min_rows=10_000, hybrid=False, reranker="none"))
    exact_store.upsert_document("bulk", "src", "u", "Bulk", "doc", chunks)
    exact_hits = exact_store.search(query, top_k=5, min_score=0.0)
    assert exact_hits
    exact_top_score = exact_hits[0].score

    # Same data, but small enough ann_min_rows that a real IVF_PQ index is built.
    indexed_store = KnowledgeStore(workspace / "indexed", FakeEmbedder(),
                                    retrieval=RetrievalConfig(ann_min_rows=200, hybrid=False, reranker="none"))
    indexed_store.upsert_document("bulk", "src", "u", "Bulk", "doc", chunks)
    indexed_store.ensure_ann_index()
    assert indexed_store._table().list_indices()  # index actually built, not skipped

    indexed_hits = indexed_store.search(query, top_k=5, min_score=0.0)
    assert indexed_hits
    # Same top hit, and its score restored to (near) the exact cosine — refine_factor
    # re-ranks the ANN candidates against their un-quantized vectors.
    assert indexed_hits[0].doc_id == exact_hits[0].doc_id
    assert abs(indexed_hits[0].score - exact_top_score) < 0.03


def test_ann_refine_factor_defaults_on_and_rejects_the_crashing_zero():
    assert RetrievalConfig().ann_refine_factor == 10
    assert RetrievalConfig(ann_refine_factor=1).ann_refine_factor == 1  # minimum-cost setting
    # LanceDB raises "Refine factor cannot be zero", so 0 would break every dense
    # search on an indexed workspace — it must be rejected at config validation, not
    # accepted and left to explode at query time.
    with pytest.raises(ValidationError):
        RetrievalConfig(ann_refine_factor=0)


def test_ann_refine_factor_one_still_searches_on_a_real_index(workspace):
    """The `ge=1` floor must actually be usable, not just non-zero."""
    store = KnowledgeStore(workspace, FakeEmbedder(),
                           retrieval=RetrievalConfig(ann_min_rows=200, hybrid=False,
                                                     reranker="none", ann_refine_factor=1))
    store.upsert_document("bulk", "src", "u", "Bulk", "doc", _bulk_chunks(400))
    store.ensure_ann_index()
    assert store.search("alpha beta gamma delta epsilon zeta", top_k=3, min_score=0.0)


# -------------------------------------------------------- contextual chunking

def test_breadcrumb_composes_source_title_path():
    assert breadcrumb("files:handbook", "Deploy Guide", "handbook/deploy.md") == (
        "files:handbook · Deploy Guide · handbook/deploy.md"
    )
    # de-duped when title == uri, and empties dropped
    assert breadcrumb("git:platform", "PaymentProcessor.cs", "PaymentProcessor.cs") == (
        "git:platform · PaymentProcessor.cs"
    )
    assert breadcrumb("wiki", "", "") == "wiki"


def test_contextual_chunking_prepends_breadcrumb_when_enabled(workspace, catalog, store):
    pipe = IngestPipeline(store, catalog, RetrievalConfig(contextual_chunks=True))
    pipe.ingest(
        [Document(uri="handbook/deploy.md", title="Deploy Guide",
                  text="We ship every Tuesday via Octopus.", kind="doc")],
        "files:handbook",
    )
    hit = store.search("octopus tuesday", top_k=1, min_score=0.0)[0]
    assert hit.text.startswith("[files:handbook · Deploy Guide")
    assert "We ship every Tuesday via Octopus." in hit.text  # raw content preserved


def test_contextual_chunking_off_without_retrieval_config(workspace, catalog, store):
    IngestPipeline(store, catalog).ingest(  # no retrieval -> raw chunks
        [Document(uri="handbook/deploy.md", title="Deploy Guide",
                  text="We ship every Tuesday via Octopus.", kind="doc")],
        "files:handbook",
    )
    hit = store.search("octopus tuesday", top_k=1, min_score=0.0)[0]
    assert hit.text == "We ship every Tuesday via Octopus."  # unchanged
