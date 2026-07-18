"""Alias query expansion: window lookup, stopword skip, cap, and wiring into search."""

import pytest

from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import RetrievalConfig
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.memory.expansion import expand_query


@pytest.fixture
def graph_catalog(catalog):
    """A catalog whose knowledge graph knows two org packages by their spoken forms."""
    catalog.upsert_entity("appriver.nautical.models", "AppRiver.Nautical.Models", "package")
    catalog.add_entity_alias("nautical models", "appriver.nautical.models")
    # A 3-token alias exercises the longest-window-first rule.
    catalog.upsert_entity("appriver.secure.gateway", "AppRiver.Secure.Gateway", "service")
    catalog.add_entity_alias("secure gateway service", "appriver.secure.gateway")
    # A 4-token alias (a leaf package) — the window cap must reach it, and must prefer
    # it over the 3-token parent that also resolves.
    catalog.upsert_entity(
        "appriver.core.healthchecks.http", "AppRiver.Core.HealthChecks.Http", "package"
    )
    catalog.add_entity_alias("core healthchecks http endpoint", "appriver.core.healthchecks.http")
    return catalog


# -- pure expand_query -----------------------------------------------------------

def test_expand_appends_canonical_name(graph_catalog):
    out = expand_query(graph_catalog, "how does nautical models auth work")
    assert out.startswith("how does nautical models auth work")
    assert "AppRiver.Nautical.Models" in out


def test_expand_no_match_returns_unchanged(graph_catalog):
    q = "how do we rotate on-call every monday"
    assert expand_query(graph_catalog, q) == q


def test_expand_prefers_longest_window(graph_catalog):
    out = expand_query(graph_catalog, "the secure gateway service is down")
    assert "AppRiver.Secure.Gateway" in out


def test_expand_matches_four_token_window(graph_catalog):
    # The 4-token spoken form must resolve to the leaf package, not just a parent.
    out = expand_query(graph_catalog, "does core healthchecks http endpoint have tests")
    assert "AppRiver.Core.HealthChecks.Http" in out


def test_expand_no_double_append_when_canonical_already_present(graph_catalog):
    # The formal name is already in the query, so its spoken-form window adds nothing.
    q = "nautical models AppRiver.Nautical.Models status"
    assert expand_query(graph_catalog, q) == q


def test_expand_skips_stopword_only_window(catalog):
    # 'models' is a generic token; a bare 'models' must not expand even if named.
    catalog.upsert_entity("models", "Models", "package")
    catalog.add_entity_alias("models", "models")
    assert expand_query(catalog, "how many models exist") == "how many models exist"


def test_expand_caps_expansions(catalog):
    for i in range(5):
        catalog.upsert_entity(f"e{i}", f"Canonical{i}", "package")
        catalog.add_entity_alias(f"alpha{i}", f"e{i}")
    q = "alpha0 alpha1 alpha2 alpha3 alpha4"
    out = expand_query(catalog, q, max_expansions=3)
    appended = out[len(q):]
    assert sum(1 for i in range(5) if f"Canonical{i}" in appended) == 3


def test_expand_empty_query():
    assert expand_query(None, "") == ""


# -- end-to-end retrieval effect (AC5) -------------------------------------------

def test_alias_expansion_retrieves_dependency_map(graph_catalog, store):
    """AC5: a spoken-form query with no literal package tokens retrieves the dependency
    map WITH expansion, but not without — the formal name is the only shared token, and
    it appears only after expansion appends it."""
    # The doc mentions only the FORMAL dotted name (no bare 'nautical'/'models' tokens).
    store.upsert_document(
        "dep1", "git:nautical", "src/nautical::dependency-map",
        "Nautical dependency map", "doc",
        ["AppRiver.Nautical.Models is the AppRiver.Nautical.Models shipping package"],
    )
    raw = store.search("nautical models", top_k=5, min_score=0.0)
    expanded = store.search(expand_query(graph_catalog, "nautical models"), top_k=5, min_score=0.0)

    raw_score = next((h.score for h in raw if h.doc_id == "dep1"), 0.0)
    exp_score = next((h.score for h in expanded if h.doc_id == "dep1"), 0.0)
    assert exp_score > 0.3          # the dependency map now clears a sensible gate
    assert exp_score > raw_score    # and expansion is what surfaced it


# -- wiring into search_memory ---------------------------------------------------

def test_search_memory_expands_before_searching(graph_catalog, store, monkeypatch):
    """search_memory expands the query (config on) before hitting the store."""
    seen = {}
    real_search = store.search

    def spy(query, **kw):
        seen["query"] = query
        return real_search(query, **kw)

    monkeypatch.setattr(store, "search", spy)
    pipe = IngestPipeline(store, graph_catalog)
    tools = {
        t.spec.name: t
        for t in build_builtin_tools(store, graph_catalog, pipe, RetrievalConfig(alias_expansion=True))
    }
    tools["search_memory"].run(query="nautical models rollout")
    assert "AppRiver.Nautical.Models" in seen["query"]


def test_search_memory_no_expansion_when_disabled(graph_catalog, store, monkeypatch):
    seen = {}
    real_search = store.search

    def spy(query, **kw):
        seen["query"] = query
        return real_search(query, **kw)

    monkeypatch.setattr(store, "search", spy)
    pipe = IngestPipeline(store, graph_catalog)
    tools = {
        t.spec.name: t
        for t in build_builtin_tools(
            store, graph_catalog, pipe, RetrievalConfig(alias_expansion=False)
        )
    }
    tools["search_memory"].run(query="nautical models rollout")
    assert seen["query"] == "nautical models rollout"  # untouched
