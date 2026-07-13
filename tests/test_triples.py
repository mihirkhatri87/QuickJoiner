"""LLM relationship extraction generalized to document ingestion: the triple→graph
conversion, the doc extractor's validation + keyless behavior, and the pipeline gate
(prose only, size threshold, code skipped, evidence = the document)."""

from __future__ import annotations

from quickjoiner.config import GraphConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.ingest.triples import Triple, extract_doc_triples, triples_to_graph
from quickjoiner.llm.base import ChatResult


class _Provider:
    """Minimal scripted provider: .chat(...) returns a fixed text block."""

    def __init__(self, text: str):
        self._text = text

    def chat(self, messages, system=None, **kw):
        return ChatResult(text=self._text)


# ---------------------------------------------------------------- conversion

def test_triples_to_graph_builds_entities_edges_and_aliases():
    g = triples_to_graph(
        [Triple("service", "checkout", "depends_on", "service", "payments")],
        "stated in Architecture",
    )
    names = {name for _id, name, _t in g["entities"]}
    assert names == {"checkout", "payments"}
    assert len(g["edges"]) == 1
    src, rel, dst, detail = g["edges"][0]
    assert rel == "depends_on" and detail == "stated in Architecture"
    assert src != dst


# ---------------------------------------------------------------- extractor

def test_extract_doc_triples_validates_and_drops_offvocab():
    provider = _Provider(
        "service: checkout | depends_on | service: payments\n"
        "just a sentence with no pipes\n"
        "widget: a | frobnicate | widget: b\n"          # off-vocabulary -> dropped
    )
    triples = extract_doc_triples(provider, "the doc body", "Arch")
    assert len(triples) == 1
    assert triples[0].src_name == "checkout" and triples[0].rel == "depends_on"


def test_extract_doc_triples_keyless_and_error_safe():
    assert extract_doc_triples(None, "body", "Title") == []  # no provider

    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no key")

    assert extract_doc_triples(Boom(), "body", "Title") == []  # provider failure -> []


# ---------------------------------------------------------------- pipeline gate

def _pipe(store, catalog, extractor, min_chars=10):
    return IngestPipeline(
        store, catalog, RetrievalConfig(),
        GraphConfig(extract_triples=True, triple_min_chars=min_chars),
        triple_extractor=extractor,
    )


def test_pipeline_persists_llm_triples_with_doc_evidence(store, catalog):
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return [Triple("service", "checkout", "depends_on", "service", "payments")]

    _pipe(store, catalog, extractor).ingest(
        [Document(uri="wiki/arch", title="Architecture",
                  text="The checkout service calls payments to settle orders.", kind="doc")],
        "confluence:eng",
    )
    assert calls == ["Architecture"]  # prose doc qualified
    checkout = catalog.resolve_entity("checkout")
    payments = catalog.resolve_entity("payments")
    assert checkout["type"] == "service"
    rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors(checkout["id"])}
    assert ("depends_on", payments["id"]) in rels


def test_pipeline_skips_code_and_short_docs(store, catalog):
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return []

    pipe = _pipe(store, catalog, extractor, min_chars=1000)
    pipe.ingest([Document(uri="a.py", title="Code", text="x = 1\n" * 400, kind="code")], "git:x")
    pipe.ingest([Document(uri="w", title="Short", text="too short", kind="doc")], "wiki:x")
    assert calls == []  # code skipped (structural extractor handles it); short doc under threshold


def test_pipeline_without_extractor_does_nothing(store, catalog):
    # No extractor supplied -> no LLM path, no error (the common/default case).
    IngestPipeline(store, catalog, RetrievalConfig()).ingest(
        [Document(uri="w", title="Doc", text="The checkout service calls payments.", kind="doc")],
        "wiki:x",
    )
    assert catalog.resolve_entity("checkout") is None
