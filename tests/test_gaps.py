"""Knowledge-debt backlog (docs/plans/01): capture on refusal, clustering,
remediation suggestions, privacy mode, resolve lifecycle, and the list_gaps tool.

FakeEmbedder (tests/conftest.py) hashes whitespace tokens into a 32-dim vector, so
identical queries have cosine 1.0 and token-disjoint queries have cosine 0.0 — that
makes clustering behavior fully deterministic here.
"""

from __future__ import annotations

from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import GapsConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.gaps import TERM_HINTS, cluster_gaps, suggest
from quickjoiner.ingest.pipeline import IngestPipeline

from tests.conftest import FakeEmbedder


def _tools(store, catalog, gaps=GapsConfig()):
    pipe = IngestPipeline(store, catalog)
    return {
        t.spec.name: t
        for t in build_builtin_tools(store, catalog, pipe, RetrievalConfig(min_score=0.55), gaps)
    }


# --------------------------------------------------------------- capture (AC1, AC5)

def test_refusal_logs_exactly_one_gap(store, catalog):
    tools = _tools(store, catalog)
    out = tools["search_memory"].run(query="where is the disaster recovery runbook")
    assert out.startswith("NO_RESULTS")
    rows = catalog.list_gaps("open")
    assert len(rows) == 1
    assert rows[0]["query"] == "where is the disaster recovery runbook"
    assert rows[0]["status"] == "open"


def test_grounded_answer_logs_no_gap(store, catalog):
    IngestPipeline(store, catalog).ingest(
        [Document(uri="w://d", title="Deploys", text="We deploy with octopus on fridays.", kind="doc")],
        "files:handbook",
    )
    tools = _tools(store, catalog)
    out = tools["search_memory"].run(query="deploy with octopus on fridays")
    assert not out.startswith("NO_RESULTS")
    assert catalog.list_gaps("open") == []


def test_no_capture_when_disabled(store, catalog):
    tools = _tools(store, catalog, gaps=GapsConfig(enabled=False))
    tools["search_memory"].run(query="anything at all here")
    assert catalog.list_gaps("open") == []


def test_capture_swallows_catalog_errors(store, catalog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(catalog, "log_gap", boom)
    tools = _tools(store, catalog)
    # The refusal must still be returned even though gap logging blows up.
    out = tools["search_memory"].run(query="this will refuse")
    assert out.startswith("NO_RESULTS")


def test_nearmiss_payload_recorded(store, catalog):
    # A doc that shares a token with the query but sits below the grounding gate.
    IngestPipeline(store, catalog).ingest(
        [Document(uri="w://p", title="Payroll", text="payroll runs monthly via finance.", kind="doc")],
        "files:hr",
    )
    tools = _tools(store, catalog)
    tools["search_memory"].run(query="payroll deployment rollback schedule details")
    row = catalog.list_gaps("open")[0]
    import json

    nearest = json.loads(row["nearest_json"])
    assert nearest and nearest[0]["source_id"] == "files:hr"
    assert "title" in nearest[0] and "score" in nearest[0]
    assert row["best_score"] >= 0.0


# --------------------------------------------------------------- privacy (AC2)

def test_hash_only_mode_omits_query(store, catalog):
    tools = _tools(store, catalog, gaps=GapsConfig(store_queries=False))
    tools["search_memory"].run(query="secret question about internal payroll systems")
    row = catalog.list_gaps("open")[0]
    assert row["query"] == ""  # never stored
    assert row["query_hash"]  # hash still present for dedupe/clustering


def test_hash_only_clustering_by_hash(catalog):
    # Two hash-only rows with the same normalized query hash cluster together;
    # a different one stays separate. (Same text -> same hash via log_gap.)
    catalog.log_gap("How do we DEPLOY", 0.1, [], store_query=False)
    catalog.log_gap("how do we deploy", 0.2, [], store_query=False)  # case/space variant
    catalog.log_gap("where is the runbook", 0.1, [], store_query=False)
    clusters = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.8, catalog)
    counts = sorted(c["count"] for c in clusters)
    assert counts == [1, 2]
    for c in clusters:
        assert c["label"] == "(private refusals)"
        assert c["suggested_connectors"] == []


# --------------------------------------------------------------- clustering (AC3)

def test_same_topic_refusals_form_one_cluster(store, catalog):
    tools = _tools(store, catalog)
    for _ in range(3):
        tools["search_memory"].run(query="how do we deploy with octopus")
    clusters = cluster_gaps(catalog.list_gaps("open"), store.embedder, 0.8, catalog)
    assert len(clusters) == 1
    assert clusters[0]["count"] == 3
    assert "octopus" in clusters[0]["suggested_connectors"]
    assert clusters[0]["gap_ids"] and len(clusters[0]["gap_ids"]) == 3


def test_distinct_topics_form_separate_clusters(catalog):
    catalog.log_gap("alpha beta gamma", 0.0, [])
    catalog.log_gap("alpha beta gamma", 0.0, [])
    catalog.log_gap("delta epsilon zeta", 0.0, [])
    clusters = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.8, catalog)
    assert [c["count"] for c in clusters] == [2, 1]  # largest first


def test_threshold_controls_grouping(catalog):
    # "octopus deploy failed" vs "octopus deploy timeout" share 2 of 3 tokens (cosine ~0.67).
    catalog.log_gap("octopus deploy failed", 0.0, [])
    catalog.log_gap("octopus deploy timeout", 0.0, [])
    tight = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.8, None)
    loose = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.6, None)
    assert len(tight) == 2  # above 0.67 threshold -> not merged
    assert len(loose) == 1  # below 0.67 threshold -> merged


def test_cluster_ordering_is_deterministic(catalog):
    for _ in range(2):
        catalog.log_gap("aaa bbb ccc", 0.0, [])
    for _ in range(3):
        catalog.log_gap("ddd eee fff", 0.0, [])
    a = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.8, None)
    b = cluster_gaps(catalog.list_gaps("open"), FakeEmbedder(), 0.8, None)
    assert [c["count"] for c in a] == [3, 2]
    assert [c["id"] for c in a] == [c["id"] for c in b]  # stable ids across runs


# --------------------------------------------------------------- suggestions

def test_suggest_covers_each_hint_family():
    assert suggest("how do we deploy the release")[0] == ["octopus"]
    assert suggest("which sprint has this ticket")[0] == ["jira"]
    assert suggest("where is the onboarding runbook wiki")[0] == ["confluence"]
    assert suggest("show me the error logs")[0] == ["grafana", "datadog", "elastic"]
    assert suggest("did the build pipeline pass")[0] == ["azure_devops", "github"]
    assert suggest("which repo has this code")[0] == ["git", "github"]
    assert suggest("what is the weather today")[0] == []  # nothing matches
    # every hint term maps to a non-empty connector list
    assert all(v for v in TERM_HINTS.values())


def test_suggest_entity_hints_from_graph(catalog):
    catalog.upsert_entity("package:appriver.nautical.models", "AppRiver.Nautical.Models", "package")
    catalog.add_entity_alias("nautical models", "package:appriver.nautical.models")
    _, hints = suggest("how does nautical models handle auth", catalog)
    assert any(h["id"] == "package:appriver.nautical.models" for h in hints)
    assert suggest("how does nautical models handle auth")[1] == []  # no catalog -> no hints


# --------------------------------------------------------------- resolve (AC4)

def test_resolve_lifecycle(catalog):
    catalog.log_gap("query one about deploys", 0.0, [])
    catalog.log_gap("query two about deploys", 0.0, [])
    open_rows = catalog.list_gaps("open")
    assert len(open_rows) == 2
    catalog.resolve_gaps([open_rows[0]["id"]], "connected:octopus")
    assert len(catalog.list_gaps("open")) == 1
    resolved = catalog.list_gaps("resolved")
    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "connected:octopus"
    assert resolved[0]["resolved_at"]


# --------------------------------------------------------------- list_gaps tool

def test_list_gaps_tool(store, catalog):
    tools = _tools(store, catalog)
    assert "No open knowledge gaps" in tools["list_gaps"].run()
    for _ in range(2):
        tools["search_memory"].run(query="how do we deploy with octopus")
    out = tools["list_gaps"].run()
    assert "2 open knowledge gap(s)" in out
    assert "octopus" in out


def test_list_gaps_tool_disabled(store, catalog):
    tools = _tools(store, catalog, gaps=GapsConfig(enabled=False))
    assert "disabled" in tools["list_gaps"].run()
