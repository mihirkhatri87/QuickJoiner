"""Rebuilding the knowledge graph from already-ingested documents.

The case: the graph extractors change but the documents do not. Re-syncing to pick that up
re-fetches everything and re-embeds whatever shifted, when the only thing that needed to
change was the edges.

The hazard, and what most of these tests are about: a connector's OWN structural claims —
ADO dev-links and hierarchy, GitLab MR joins, Jira issue links, Octopus deployments — are
computed while FETCHING, so skipping the fetch is exactly what loses them. Measured on a real
100k-edge graph, **27% of all edges** could not be re-derived from stored text. Two defences
are pinned here: the payload is captured at ingest so a rebuild can be faithful, and a
document that never got one has its existing edges PRESERVED rather than deleted — because
`replace_doc_edges` is a delete-then-insert, so anything not handed back is gone.
"""

from __future__ import annotations

import pytest

from quickjoiner.config import GraphConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import GRAPH_EXTRACTOR_VERSION, IngestPipeline, _doc_id

SRC = "azure_devops:tfs"

# A work item carrying the connector's own dev-link — the shape that cannot be re-derived
# from prose, because TFS *stated* it in an artifact link rather than writing it in the text.
DEV_LINK_GRAPH = {
    "entities": [["ticket:#1", "#1", "ticket"], ["repo:connector", "Connector", "repo"]],
    "aliases": [],
    "edges": [["ticket:#1", "implemented_in", "repo:connector", "dev-link"]],
}


def _doc(text: str = "The connector was upgraded. Team Caffeine owns this work.",
         graph: dict | None = DEV_LINK_GRAPH) -> Document:
    return Document(uri="https://tfs/wi/1", title="Work item 1", text=text, kind="doc",
                    metadata={"graph": graph} if graph else {})


def _pipe(store, catalog, **kw) -> IngestPipeline:
    return IngestPipeline(store, catalog, RetrievalConfig(), **kw)


@pytest.fixture
def ingested(store, catalog):
    pipe = _pipe(store, catalog)
    doc = _doc()
    pipe.ingest([doc], SRC)
    return pipe, _doc_id(SRC, doc.uri)


def _rels(catalog, doc_id) -> set[str]:
    return {e[1] for e in catalog.edges_for_document(doc_id)}


# ------------------------------------------------- capturing the payload

def test_ingest_stores_the_connector_payload_so_a_rebuild_needs_no_round_trip(ingested, catalog):
    _pipe_, doc_id = ingested
    row = catalog.documents_for_source(SRC)[0]
    assert row["doc_id"] == doc_id
    assert "implemented_in" in (row["graph_json"] or "")


def test_a_connector_asserting_nothing_stores_nothing(store, catalog):
    _pipe(store, catalog).ingest([_doc(graph=None)], SRC)
    assert catalog.documents_for_source(SRC)[0]["graph_json"] == ""


def test_an_unchanged_document_backfills_its_payload_without_re_embedding(store, catalog):
    """The migration path for a corpus ingested before the column existed: one ordinary sync
    captures the payload, and only then can a rebuild be faithful."""
    pipe = _pipe(store, catalog)
    doc = _doc()
    pipe.ingest([doc], SRC)
    doc_id = _doc_id(SRC, doc.uri)
    catalog.set_document_graph_payload(doc_id, "")          # simulate a legacy document
    catalog.set_document_graph_version(doc_id, 0)           # ...and a stale extractor version

    stats = pipe.ingest([doc], SRC)                          # same content: no re-embed
    assert stats.chunks == 0 and stats.skipped == 1
    assert "implemented_in" in (catalog.documents_for_source(SRC)[0]["graph_json"] or "")


# ------------------------------------------------------- rebuilding

def test_a_faithful_rebuild_reproduces_the_connector_edges(ingested, catalog):
    pipe, doc_id = ingested
    stats = pipe.rebuild_graph(source_id=SRC)
    assert (stats.documents, stats.faithful, stats.preserved) == (1, 1, 0)
    assert "implemented_in" in _rels(catalog, doc_id)


def test_a_document_with_no_payload_keeps_the_edges_the_rebuild_cannot_derive(ingested, catalog):
    """The safety property this feature turns on. `replace_doc_edges` deletes then inserts,
    so a rebuild that simply re-derived from text would silently drop 27% of a real graph."""
    pipe, doc_id = ingested
    catalog.set_document_graph_payload(doc_id, "")           # a pre-existing document
    assert "implemented_in" in _rels(catalog, doc_id)        # ...that HAS the edge today

    stats = pipe.rebuild_graph(source_id=SRC)
    assert (stats.documents, stats.faithful, stats.preserved) == (1, 0, 1)
    assert "implemented_in" in _rels(catalog, doc_id), "un-derivable connector edge was lost"


def test_a_rebuild_picks_up_a_newly_taught_deterministic_edge(store, catalog):
    """The point of the whole feature: an extractor improves, and the graph gains the edges
    it now finds — from stored text, with no re-fetch and no re-embed."""
    pipe = _pipe(store, catalog)
    table = ("Members:\n| Name | Email |\n| --- | --- |\n"
             "| Liu Maumasi | lm@corp.com |\n")
    doc = Document(uri="https://plumber/TeamDetails?team=Caffeine", title="Caffeine",
                   text=table, kind="doc")
    pipe.ingest([doc], "web_scrape:plumber")
    doc_id = _doc_id("web_scrape:plumber", doc.uri)
    catalog.replace_doc_edges(doc_id, [])                     # pretend it was built before
    assert _rels(catalog, doc_id) == set()

    pipe.rebuild_graph(source_id="web_scrape:plumber")
    assert "works_on" in _rels(catalog, doc_id)


def test_a_document_whose_text_is_gone_is_left_exactly_as_it_was(ingested, store, catalog):
    """Rebuilding it into an empty graph would delete real edges over missing input."""
    pipe, doc_id = ingested
    store.delete_document(doc_id)
    stats = pipe.rebuild_graph(source_id=SRC)
    assert (stats.documents, stats.missing_text) == (0, 1)
    assert "implemented_in" in _rels(catalog, doc_id)


def test_an_unscoped_rebuild_covers_every_source(store, catalog):
    pipe = _pipe(store, catalog)
    pipe.ingest([_doc()], SRC)
    pipe.ingest([Document(uri="https://wiki/p", title="P", text="Some prose about a team.",
                          kind="doc")], "confluence:eng")
    assert pipe.rebuild_graph().documents == 2


def test_scoping_leaves_another_source_untouched(store, catalog):
    pipe = _pipe(store, catalog)
    pipe.ingest([_doc()], SRC)
    pipe.ingest([Document(uri="https://wiki/p", title="P", text="Prose.", kind="doc")],
                "confluence:eng")
    assert pipe.rebuild_graph(source_id="confluence:eng").documents == 1


def test_rebuilding_stamps_the_extractor_version_so_a_later_sync_does_not_redo_it(
    ingested, catalog
):
    _pipe_, doc_id = ingested
    catalog.set_document_graph_version(doc_id, 0)
    _pipe_.rebuild_graph(source_id=SRC)
    assert catalog.get_document_graph_version(doc_id) == GRAPH_EXTRACTOR_VERSION


# ------------------------------------------------ the LLM half is opt-in

def test_the_default_rebuild_spends_no_llm_calls(store, catalog):
    """One model call per document is hours on a real corpus, so it must be asked for."""
    calls: list = []
    pipe = _pipe(store, catalog, graph=GraphConfig(extract_triples=True, triple_min_chars=1),
                 triple_extractor=lambda text, title, guidance="": calls.append(title) or [])
    pipe.ingest([_doc()], SRC)
    calls.clear()

    stats = pipe.rebuild_graph(source_id=SRC)
    assert calls == []
    assert stats.queued_triples == 0 and stats.documents == 1


def test_with_triples_re_queues_relationship_mining(store, catalog):
    calls: list = []
    pipe = _pipe(store, catalog, graph=GraphConfig(extract_triples=True, triple_min_chars=1),
                 triple_extractor=lambda text, title, guidance="": calls.append(title) or [])
    pipe.ingest([_doc()], SRC)
    calls.clear()

    stats = pipe.rebuild_graph(source_id=SRC, with_triples=True)
    assert calls == ["Work item 1"]
    assert stats.queued_triples == 1


# --------------------------------------------------------- sizing reads

def test_the_preview_counts_what_cannot_be_rebuilt_in_full(ingested, catalog):
    _pipe_, doc_id = ingested
    assert catalog.count_documents(SRC) == 1
    assert catalog.documents_missing_graph_payload(SRC) == 0
    catalog.set_document_graph_payload(doc_id, "")
    assert catalog.documents_missing_graph_payload(SRC) == 1
    assert catalog.documents_missing_graph_payload() == 1
