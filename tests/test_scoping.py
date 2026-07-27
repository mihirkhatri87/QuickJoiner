"""Scoped questions: labels (tags/aka) over ingested documents, and the retrieval +
tool narrowing they drive.

The point of scoping is that it does LESS work, not that it hides results afterwards — so
these tests assert the filter reaches the query (both hybrid legs), that a folder label is a
live prefix rule rather than a snapshot, and that a scoped turn withholds live tools for
sources the user excluded.
"""

from __future__ import annotations

import pytest

from quickjoiner.memory.catalog import Catalog, label_applies
from quickjoiner.memory.store import KnowledgeStore, SearchScope
from tests.conftest import FakeEmbedder


# ------------------------------------------------------------------ pure label rules

@pytest.mark.parametrize("uri, prefix, expected", [
    ("file:///d/docs/arch/deck.pptx", "", True),                       # whole connector
    ("file:///d/docs/arch/deck.pptx", "file:///d/docs/arch/", True),   # inside the folder
    ("file:///d/docs/other/deck.pptx", "file:///d/docs/arch/", False),  # different folder
    ("file:///d/docs/arch/deck.pptx", "file:///d/docs/arch/deck.pptx", True),  # exact doc
])
def test_label_applies_covers_connector_folder_and_document(uri, prefix, expected):
    assert label_applies(uri, prefix) is expected


def test_search_scope_is_a_union_and_empty_means_everything():
    empty = SearchScope()
    assert empty.is_empty() and empty.matches("any:source", "anydoc")
    scope = SearchScope(source_ids=["uploads:uploads"], doc_ids=["d9"])
    assert scope.matches("uploads:uploads", "whatever")   # by source
    assert scope.matches("other:src", "d9")               # by document
    assert not scope.matches("other:src", "d1")


# ------------------------------------------------------------------- catalog layer

@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(tmp_path / "ws")
    for i, (doc_id, source_id, uri) in enumerate([
        ("d-arch-1", "uploads:uploads", "file:///d/docs/arch/roadmap.pptx"),
        ("d-arch-2", "uploads:uploads", "file:///d/docs/arch/mx.pptx"),
        ("d-hr-1", "uploads:uploads", "file:///d/docs/hr/policy.docx"),
        ("d-git-1", "git:connector", "https://git/repo::README.md"),
    ]):
        cat.upsert_document(doc_id, source_id, uri, f"doc {i}", "doc", f"hash{i}", None, 1)
    yield cat
    cat.close()


def test_folder_tag_covers_documents_ingested_after_it_was_set(catalog):
    """The behaviour that makes folder tags worth having: tag once, and later files inherit."""
    catalog.set_label("uploads:uploads", "file:///d/docs/arch/", "tag", "architecture")
    _sources, docs = catalog.resolve_scope(tags=["architecture"])
    assert set(docs) == {"d-arch-1", "d-arch-2"}

    # A file ingested LATER into the same folder is covered with no re-tagging.
    catalog.upsert_document("d-arch-3", "uploads:uploads",
                            "file:///d/docs/arch/new-deck.pptx", "new", "doc", "h9", None, 1)
    _sources, docs = catalog.resolve_scope(tags=["architecture"])
    assert "d-arch-3" in docs
    assert "d-hr-1" not in docs  # and a sibling folder is still excluded


def test_connector_wide_tag_resolves_to_a_source_not_an_enumeration(catalog):
    """A whole-connector tag must stay a cheap `source_id IN (...)` predicate — enumerating
    every document would make the filter scale with corpus size for no benefit."""
    catalog.set_label("uploads:uploads", "", "tag", "internal")
    sources, docs = catalog.resolve_scope(tags=["internal"])
    assert sources == ["uploads:uploads"]
    assert docs == []


def test_labels_are_idempotent_and_removable(catalog):
    catalog.set_label("uploads:uploads", "", "tag", "internal")
    catalog.set_label("uploads:uploads", "", "tag", "internal")  # same again
    assert len(catalog.labels_for_source("uploads:uploads")) == 1
    catalog.remove_label("uploads:uploads", "", "tag", "internal")
    assert catalog.labels_for_source("uploads:uploads") == []


def test_a_path_containing_sql_wildcards_does_not_widen_the_match(catalog):
    """A real Windows path or URL can contain `%` or `_`; unescaped they are LIKE wildcards
    and would silently pull in documents the user never tagged."""
    catalog.upsert_document("d-pct", "uploads:uploads",
                            "file:///d/docs/100%_plans/a.md", "pct", "doc", "hx", None, 1)
    catalog.upsert_document("d-other", "uploads:uploads",
                            "file:///d/docs/100XYplans/b.md", "other", "doc", "hy", None, 1)
    catalog.set_label("uploads:uploads", "file:///d/docs/100%_plans/", "tag", "plans")
    _sources, docs = catalog.resolve_scope(tags=["plans"])
    assert docs == ["d-pct"]


def test_explicit_sources_and_docs_pass_through_and_dedupe(catalog):
    sources, docs = catalog.resolve_scope(
        source_ids=["git:connector", "git:connector"], doc_ids=["d-hr-1", "d-hr-1"])
    assert sources == ["git:connector"] and docs == ["d-hr-1"]


# --------------------------------------------------------------- retrieval filtering

@pytest.fixture
def store(tmp_path):
    from quickjoiner.config import RetrievalConfig

    st = KnowledgeStore(tmp_path / "lance", FakeEmbedder(),
                        retrieval=RetrievalConfig(hybrid=True, reranker="none"))
    st.upsert_document(
        doc_id="d-arch", source_id="uploads:uploads", uri="file:///arch/roadmap.pptx",
        title="Zix architecture roadmap", kind="doc", updated_at=None,
        chunks=["the gateway routes mail through securetide"])
    st.upsert_document(
        doc_id="d-git", source_id="git:connector", uri="https://git/repo::README.md",
        title="Connector README", kind="doc", updated_at=None,
        chunks=["the gateway routes mail through securetide"])
    return st


def test_scoped_search_returns_only_in_scope_hits(store):
    """Identical text in two sources: unscoped finds both, scoped finds exactly one."""
    everything = store.search("gateway routes mail", top_k=10)
    assert {h.source_id for h in everything} == {"uploads:uploads", "git:connector"}

    scoped = store.search("gateway routes mail", top_k=10,
                          scope=SearchScope(source_ids=["uploads:uploads"]))
    assert scoped and {h.source_id for h in scoped} == {"uploads:uploads"}


def test_scoping_by_document_id_works_across_both_legs(store):
    scoped = store.search("gateway routes mail", top_k=10,
                          scope=SearchScope(doc_ids=["d-git"]))
    assert scoped and {h.doc_id for h in scoped} == {"d-git"}


def test_an_empty_scope_is_byte_identical_to_no_scope(store):
    plain = store.search("gateway routes mail", top_k=10)
    empty = store.search("gateway routes mail", top_k=10, scope=SearchScope())
    assert [(h.doc_id, round(h.score, 6)) for h in plain] == \
           [(h.doc_id, round(h.score, 6)) for h in empty]


def test_a_scope_matching_nothing_returns_nothing_rather_than_falling_back(store):
    """The dangerous failure would be silently widening to the whole corpus."""
    assert store.search("gateway routes mail", top_k=10,
                        scope=SearchScope(source_ids=["nope:none"])) == []


def test_a_connector_name_with_a_quote_cannot_break_the_predicate(store):
    """source_id embeds a user-chosen connector name, so it reaches a LanceDB SQL string."""
    hits = store.search("gateway routes mail", top_k=5,
                        scope=SearchScope(source_ids=["files:o'brien notes"]))
    assert hits == []  # no match, and crucially no SQL error


# ------------------------------------------------------ agent-level narrowing (the payoff)

def test_a_scoped_turn_withholds_live_tools_for_excluded_sources(tmp_path, monkeypatch):
    """The half that actually saves round-trips: the model cannot call into a system the
    user scoped out, because those tools are never offered."""
    import quickjoiner.app as app_module
    from quickjoiner.config import SourceConfig
    from quickjoiner.llm.base import AgentTool, ToolSpec

    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ctx = app_module.build_context(tmp_path / "ws")
    ctx.config.sources = [
        SourceConfig(name="uploads", type="uploads", options={}),
        SourceConfig(name="zix", type="gitlab", options={"project": "a/b", "token": "t"}),
    ]

    def fake_connector_tools(sources=None):
        return [
            AgentTool(spec=ToolSpec(name=f"live_{s.name}", description="d",
                                    input_schema={"type": "object", "properties": {}}),
                      fn=lambda: "")
            for s in (sources or [])
        ]

    monkeypatch.setattr(ctx, "connector_tools", fake_connector_tools)
    monkeypatch.setattr(ctx, "build_provider", lambda *a, **k: object())

    # The API always passes an explicit visible-source list; mirror that here.
    unscoped = set(ctx.build_agent(sources=ctx.config.sources)._tools)
    assert {"live_uploads", "live_zix"} <= unscoped

    scoped = set(ctx.build_agent(sources=ctx.config.sources,
                                 scope=SearchScope(source_ids=["uploads:uploads"]))._tools)
    assert "live_uploads" in scoped
    assert "live_zix" not in scoped          # the excluded system is unreachable this turn
    assert "search_memory" in scoped         # memory itself is still available, just filtered


def test_a_scoped_refusal_says_it_only_searched_a_slice(tmp_path, monkeypatch):
    """A scoped 'no results' is a different claim from a global one. Reporting it as
    'never learned' would be a false statement about the organisation."""
    import quickjoiner.app as app_module
    from quickjoiner.agent.tools import build_builtin_tools
    from quickjoiner.config import RetrievalConfig

    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ctx = app_module.build_context(tmp_path / "ws")
    tools = build_builtin_tools(
        ctx.store, ctx.catalog, ctx.pipeline, RetrievalConfig(),
        scope=SearchScope(source_ids=["uploads:uploads"]))
    search = next(t for t in tools if t.spec.name == "search_memory")
    out = search.fn(query="anything at all")
    assert "NO_RESULTS" in out
    assert "scoped" in out and "do NOT claim" in out.replace("do NOT", "do NOT")
