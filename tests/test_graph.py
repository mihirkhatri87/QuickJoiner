"""Knowledge graph Phase A: catalog entity/edge storage, extractors in the
ingest pipeline (dependency maps + ticket references), alias resolution, and
the graph_neighbors agent tool."""

from __future__ import annotations

from quickjoiner.agent.tools import build_builtin_tools
from quickjoiner.config import RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.connectors.files import FilesConnector
from quickjoiner.ingest.pipeline import IngestPipeline, source_entity, ticket_keys

from tests.test_deps import CSPROJ_A, CSPROJ_B, _write


# ------------------------------------------------------------------ catalog

def test_catalog_entity_alias_resolution(catalog):
    catalog.upsert_entity("package:appriver.nautical.models", "AppRiver.Nautical.Models",
                          "package", "files:b")
    catalog.add_entity_alias("Nautical Models", "package:appriver.nautical.models")

    by_name = catalog.resolve_entity("appriver.nautical.MODELS")
    by_alias = catalog.resolve_entity("nautical models")
    by_id = catalog.resolve_entity("package:appriver.nautical.models")
    assert by_name and by_alias and by_id
    assert by_alias["id"] == "package:appriver.nautical.models"
    assert catalog.resolve_entity("does-not-exist") is None


def test_upsert_entity_keeps_incumbent_name_over_worse_cased_variant(catalog):
    # Regression (plan 05 C4 leg, 2026-07-28): a deterministic extractor names a node
    # once ('Nautical'); a later LLM-proposed triple for the SAME entity id must not
    # silently downgrade the display name to a lowercase guess ('nautical').
    catalog.upsert_entity("repo:nautical", "Nautical", "repo", "git:nautical")
    catalog.upsert_entity("repo:nautical", "nautical", "repo", "git:nautical")
    assert catalog.resolve_entity("repo:nautical")["name"] == "Nautical"

    # A better-cased variant than the incumbent DOES win.
    catalog.upsert_entity("repo:stevedore", "stevedore", "repo", "git:stevedore")
    catalog.upsert_entity("repo:stevedore", "Stevedore", "repo", "git:stevedore")
    assert catalog.resolve_entity("repo:stevedore")["name"] == "Stevedore"

    # A genuinely different name (not just a casing variant) still replaces — a real
    # rename/correction must not be blocked by this guard.
    catalog.upsert_entity("repo:renamed", "Old Name", "repo", "git:renamed")
    catalog.upsert_entity("repo:renamed", "New Name", "repo", "git:renamed")
    assert catalog.resolve_entity("repo:renamed")["name"] == "New Name"


def test_graph_snapshot_balances_across_types_not_one_numerous_type(catalog):
    # Regression: after a GitLab sync the many `branch:` entities (sorting before every other
    # type) monopolized the whole-graph snapshot budget, rendering it as branches+tickets only
    # and hiding services/repos/deps. The snapshot must balance across entity TYPES so a
    # numerous type can't crowd everything else out.
    for i in range(40):
        catalog.upsert_entity(f"branch:r/b{i}", f"b{i}", "branch")
    for eid, name, typ in [("repo:r", "R", "repo"), ("service:s", "S", "service"),
                           ("package:p", "P", "package"), ("environment:e", "E", "environment")]:
        catalog.upsert_entity(eid, name, typ)
    edges = [(f"branch:r/b{i}", "belongs_to", "repo:r", "") for i in range(40)]
    edges += [("service:s", "depends_on", "package:p", ""),
              ("service:s", "deploys", "environment:e", "")]
    catalog.replace_doc_edges("d", edges)

    snap = catalog.graph_snapshot(None, limit=20)
    types = {n["type"] for n in snap["nodes"]}
    # branches would fill a 20-edge budget alone; type balancing must surface the rest
    assert "service" in types and "repo" in types
    assert {"package", "environment"} <= types
    # branches still appear (they're not suppressed) — just capped to a fair share
    assert "branch" in types


def test_catalog_edges_replace_and_cascade(catalog):
    catalog.upsert_document("mapdoc", "files:a", "u", "a: dependencies & packages",
                            "doc", "h1", None, 1)
    catalog.upsert_entity("repo:a", "a", "repo")
    catalog.upsert_entity("package:x", "X", "package")
    catalog.replace_doc_edges("mapdoc", [
        ("repo:a", "depends_on", "package:x", "1.0"),
        ("repo:a", "provides", "package:x", "oops"),
    ])
    assert len(catalog.graph_neighbors("repo:a")) == 2

    # replace-on-reingest: the doc now asserts one edge; the stale one goes
    catalog.replace_doc_edges("mapdoc", [("repo:a", "depends_on", "package:x", "2.0")])
    rows = catalog.graph_neighbors("repo:a")
    assert len(rows) == 1 and rows[0]["detail"] == "2.0"
    assert rows[0]["evidence_title"] == "a: dependencies & packages"  # documents join

    catalog.delete_document("mapdoc")  # edge lifecycle follows the evidence doc
    assert catalog.graph_neighbors("repo:a") == []


def test_edge_evidence_carries_document_kind(catalog):
    """The frontend uses evidence.kind to decide whether an evidence chip may
    try a local-file view (code) or must behave exactly like a plain external
    link (doc, e.g. Confluence) — graph_neighbors/graph_snapshot/graph_path all
    share _EDGE_SELECT, so this locks the contract in one place."""
    catalog.upsert_document("code-doc", "git:x", "u1", "Api.cs", "code", "h1", None, 1)
    catalog.upsert_document("wiki-doc", "confluence:x", "u2", "Some Page", "doc", "h2", None, 1)
    catalog.upsert_entity("repo:x", "x", "repo")
    catalog.upsert_entity("service:y", "y", "service")
    catalog.replace_doc_edges("code-doc", [("repo:x", "defines", "service:y", "")])
    rows = catalog.graph_neighbors("repo:x")
    assert rows[0]["evidence_kind"] == "code"

    catalog.replace_doc_edges("wiki-doc", [("repo:x", "references", "service:y", "")])
    rows = {r["evidence_doc_id"]: r for r in catalog.graph_neighbors("repo:x")}
    assert rows["code-doc"]["evidence_kind"] == "code"
    assert rows["wiki-doc"]["evidence_kind"] == "doc"

    snap = catalog.graph_snapshot("repo:x")
    kinds = {e["evidence"]["doc_id"]: e["evidence"]["kind"] for e in snap["edges"]}
    assert kinds == {"code-doc": "code", "wiki-doc": "doc"}


def test_catalog_graph_pending_roundtrip(catalog):
    catalog.mark_graph_pending("doc1", "confluence:eng")
    catalog.mark_graph_pending("doc2", "confluence:eng")
    assert catalog.is_graph_pending("doc1") is True
    assert catalog.is_graph_pending("doc-unknown") is False
    assert catalog.count_graph_pending("confluence:eng") == 2
    assert catalog.count_graph_pending() == 2

    catalog.mark_graph_pending("doc1", "confluence:eng")  # idempotent re-mark
    assert catalog.count_graph_pending() == 2

    catalog.clear_graph_pending("doc1")
    assert catalog.is_graph_pending("doc1") is False
    assert catalog.count_graph_pending("confluence:eng") == 1

    catalog.upsert_document("doc2", "confluence:eng", "u", "t", "doc", "h", None, 1)
    catalog.delete_document("doc2")  # document lifecycle also clears graph_pending
    assert catalog.is_graph_pending("doc2") is False


def test_catalog_search_entities_matches_name_and_alias(catalog):
    catalog.upsert_entity("service:appriver-nautical", "AppRiver Nautical", "service")
    catalog.add_entity_alias("nautical models", "service:appriver-nautical")
    catalog.upsert_entity("service:unrelated", "Something Else Entirely", "service")

    by_name = catalog.search_entities("nautical")
    assert {r["id"] for r in by_name} == {"service:appriver-nautical"}

    by_alias = catalog.search_entities("nautical models")
    assert {r["id"] for r in by_alias} == {"service:appriver-nautical"}

    assert catalog.search_entities("") == []
    assert catalog.search_entities("zzz-nothing-matches") == []


def test_catalog_bridge_entities_needs_two_distinct_sources(catalog):
    catalog.upsert_document("d1", "git:repo", "u1", "t1", "doc", "h1", None, 1)
    catalog.upsert_document("d2", "confluence:wiki", "u2", "t2", "doc", "h2", None, 1)
    catalog.upsert_document("d3", "git:repo", "u3", "t3", "doc", "h3", None, 1)
    catalog.upsert_entity("service:shared", "Shared", "service")
    catalog.upsert_entity("service:repo-only", "RepoOnly", "service")

    catalog.replace_doc_edges("d1", [("repo:a", "depends_on", "service:shared", "")])
    catalog.replace_doc_edges("d2", [("service:shared", "part_of", "service:other", "")])
    catalog.replace_doc_edges("d3", [("repo:a", "depends_on", "service:repo-only", "")])

    bridges = catalog.bridge_entities()
    ids = {r["id"] for r in bridges}
    assert "service:shared" in ids  # touched by both git and confluence evidence
    assert "service:repo-only" not in ids  # only ever touched by git evidence
    row = next(r for r in bridges if r["id"] == "service:shared")
    assert row["source_count"] == 2


def test_catalog_graph_snapshot(catalog):
    catalog.upsert_entity("repo:a", "a", "repo")
    catalog.upsert_entity("package:x", "X", "package")
    catalog.replace_doc_edges("d1", [("repo:a", "depends_on", "package:x", "")])
    snap = catalog.graph_snapshot()
    assert {n["id"] for n in snap["nodes"]} == {"repo:a", "package:x"}
    assert snap["edges"][0]["rel"] == "depends_on"
    scoped = catalog.graph_snapshot("package:x")
    assert {n["id"] for n in scoped["nodes"]} == {"repo:a", "package:x"}


# --------------------------------------------------------------- extractors

def test_ticket_key_extraction():
    text = ("Fixed NAUT-123 and PAY-7 after the UTF-8 regression; see CVE-2024-1234, "
            "NAUT-123 again, and SHA-256 hashing notes.")
    assert ticket_keys(text) == ["NAUT-123", "PAY-7"]


def test_source_entity_mapping():
    assert source_entity("git:proj-a") == ("repo:proj-a", "proj-a", "repo")
    assert source_entity("files:proj-a") == ("repo:proj-a", "proj-a", "repo")
    assert source_entity("notes:user-taught") == ("source:user-taught", "user-taught", "source")


def test_pipeline_builds_dependency_graph(tmp_path, workspace, catalog, store):
    """The user scenario, structurally: after ingesting consumer + provider repos,
    the org alias resolves to the package entity and both edges are recorded
    with the dependency maps as evidence."""
    _write(tmp_path / "a", "src/Api/Api.csproj", CSPROJ_A)
    _write(tmp_path / "b", "Models/Models.csproj", CSPROJ_B)
    pipe = IngestPipeline(store, catalog)
    for name, folder in (("proj-a", "a"), ("nautical-models", "b")):
        connector = FilesConnector(name=name, options={"path": str(tmp_path / folder)},
                                   workspace=workspace)
        pipe.ingest(connector.sync({}), f"files:{name}")

    ent = catalog.resolve_entity("nautical models")
    assert ent and ent["id"] == "package:appriver.nautical.models"

    rows = catalog.graph_neighbors(ent["id"])
    rels = {(r["src"], r["rel"]) for r in rows}
    assert ("repo:proj-a", "depends_on") in rels
    assert ("repo:nautical-models", "provides") in rels
    for r in rows:
        assert r["evidence_uri"].endswith("::dependency-map")


def test_pipeline_extracts_and_refreshes_ticket_edges(catalog, store):
    pipe = IngestPipeline(store, catalog)
    doc = Document(uri="w://note", title="Deploy note",
                   text="NAUT-123 was fixed by bumping the retry budget in the gateway.",
                   kind="doc")
    pipe.ingest([doc], "git:proj-a")
    ticket = catalog.resolve_entity("NAUT-123")
    assert ticket and ticket["type"] == "ticket"
    rows = catalog.graph_neighbors("ticket:naut-123")
    assert rows and rows[0]["src"] == "repo:proj-a" and rows[0]["rel"] == "references"

    # the doc changes and no longer mentions the ticket -> the edge is dropped
    doc2 = Document(uri="w://note", title="Deploy note",
                    text="The retry budget note was superseded by the new runbook.",
                    kind="doc")
    pipe.ingest([doc2], "git:proj-a")
    assert catalog.graph_neighbors("ticket:naut-123") == []


# -------------------------------------------------------------- graph_path

def test_catalog_graph_path_bfs(catalog):
    # chain: repo:a --depends_on--> package:x <--provides-- repo:b --references--> ticket:t-1
    for eid, name, type_ in (("repo:a", "a", "repo"), ("repo:b", "b", "repo"),
                             ("package:x", "X", "package"), ("ticket:t-1", "T-1", "ticket")):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("d1", [("repo:a", "depends_on", "package:x", "")])
    catalog.replace_doc_edges("d2", [("repo:b", "provides", "package:x", "")])
    catalog.replace_doc_edges("d3", [("repo:b", "references", "ticket:t-1", "")])

    path = catalog.graph_path("repo:a", "ticket:t-1")
    assert path is not None and [p["rel"] for p in path] == ["depends_on", "provides", "references"]
    assert catalog.graph_path("repo:a", "ticket:t-1", max_hops=2) is None  # hop cap honored
    assert catalog.graph_path("repo:a", "repo:a") == []
    assert catalog.graph_path("repo:a", "service:unconnected") is None


def test_graph_path_bridges_pubsub_runtime_coupling(catalog):
    """The manifest blind spot: two services wired only through a Service Bus topic
    share NO package dependency, so deps.py can never link them — the pub/sub verbs
    are what connect them. checkout --publishes_to--> order-events <--subscribes_to--
    billing, plus billing --stores_in--> BillingDb for the storage hop."""
    for eid, name, type_ in (
        ("service:checkout", "Checkout", "service"),
        ("service:billing", "Billing", "service"),
        ("topic:order-events", "order-events", "topic"),
        ("datastore:billingdb", "BillingDb", "datastore"),
    ):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("wiki1", [("service:checkout", "publishes_to", "topic:order-events", "")])
    catalog.replace_doc_edges("wiki2", [("service:billing", "subscribes_to", "topic:order-events", "")])
    catalog.replace_doc_edges("wiki3", [("service:billing", "stores_in", "datastore:billingdb", "")])

    # "If I change the order-events payload, who breaks?" — the impact-analysis path.
    path = catalog.graph_path("service:checkout", "service:billing")
    assert path is not None and [p["rel"] for p in path] == ["publishes_to", "subscribes_to"]
    # "Where does the event's data end up?" — extend one storage hop.
    path = catalog.graph_path("service:checkout", "datastore:billingdb")
    assert path is not None and [p["rel"] for p in path] == [
        "publishes_to", "subscribes_to", "stores_in"]


def test_catalog_graph_path_scans_the_whole_edge_table(catalog):
    """graph_path's BFS must see every edge, not a truncated prefix of them — a
    "no known path" answer is treated everywhere as an honest refusal, so a
    silent row cap could make that refusal wrong (a real path existing just
    outside the truncated set). Seed well past what an old hardcoded LIMIT
    would have covered, with the real chain deliberately written LAST, so the
    test would have failed under the previous unordered `LIMIT 10000`-style cap
    if it were re-introduced at a smaller threshold."""
    for i in range(200):
        catalog.upsert_entity(f"symbol:noise{i}", f"Noise{i}", "symbol")
        catalog.replace_doc_edges(f"noise-doc-{i}", [(f"symbol:noise{i}", "references", "ticket:decoy", "")])

    catalog.upsert_entity("repo:late-a", "LateA", "repo")
    catalog.upsert_entity("package:late-x", "LateX", "package")
    catalog.replace_doc_edges("late-doc", [("repo:late-a", "depends_on", "package:late-x", "")])

    path = catalog.graph_path("repo:late-a", "package:late-x")
    assert path is not None and path[0]["rel"] == "depends_on"


# ------------------------------------------------- connector metadata entities

def test_jira_issue_document_emits_graph_meta():
    from quickjoiner.connectors.jira import issue_document

    issue = {"key": "PAY-7", "fields": {"summary": "Fix checkout", "status": {"name": "Done"},
                                        "issuetype": {"name": "Story"},
                                        "parent": {"key": "PAY-1"}}}
    graph = issue_document("https://jira.test", issue).metadata["graph"]
    assert ("ticket:pay-7", "PAY-7", "ticket") in graph["entities"]
    assert ("project:pay", "PAY", "project") in graph["entities"]
    assert ("ticket:pay-7", "part_of", "project:pay", "Fix checkout") in graph["edges"]
    assert ("ticket:pay-7", "part_of", "ticket:pay-1", "epic/parent") in graph["edges"]


def test_octopus_documents_emit_graph_meta():
    from quickjoiner.connectors.octopus import dashboard_document, project_document

    dash = dashboard_document(
        "https://octo.test",
        [{"ProjectId": "P1", "EnvironmentId": "E1", "ReleaseVersion": "1.2.3", "State": "Success"}],
        {"P1": "Payments API"}, {"E1": "Production"},
    )
    graph = dash.metadata["graph"]
    assert ("service:payments api", "Payments API", "service") in graph["entities"]
    assert ("environment:production", "Production", "environment") in graph["entities"]
    assert ("service:payments api", "deploys", "environment:production",
            "1.2.3 — Success") in graph["edges"]

    proj = project_document("https://octo.test", {"Name": "Payments API", "Slug": "pay"})
    assert ("service:payments api", "Payments API", "service") in proj.metadata["graph"]["entities"]


# --------------------------------------------------------------- agent tool

def _tools(store, catalog):
    pipe = IngestPipeline(store, catalog)
    return {t.spec.name: t for t in build_builtin_tools(store, catalog, pipe, RetrievalConfig())}


def _graph_tool(store, catalog):
    return _tools(store, catalog)["graph_neighbors"]


def test_graph_path_tool_chains_with_evidence(tmp_path, workspace, catalog, store):
    """'How is proj-a related to NAUT-9?' — consumer repo -> package -> provider
    repo -> ticket, three hops, every hop evidenced."""
    _write(tmp_path / "a", "src/Api/Api.csproj", CSPROJ_A)
    _write(tmp_path / "b", "Models/Models.csproj", CSPROJ_B)
    pipe = IngestPipeline(store, catalog)
    for name, folder in (("proj-a", "a"), ("nautical-models", "b")):
        connector = FilesConnector(name=name, options={"path": str(tmp_path / folder)},
                                   workspace=workspace)
        pipe.ingest(connector.sync({}), f"files:{name}")
    pipe.ingest([Document(uri="w://n", title="Release note",
                          text="NAUT-9 shipped in the models package release.", kind="doc")],
                "files:nautical-models")

    out = _tools(store, catalog)["graph_path"].run(a="proj-a", b="NAUT-9")
    assert "3 hop(s)" in out
    assert "--depends_on-->" in out and "--provides-->" in out and "--references-->" in out
    assert "[evidence:" in out

    catalog.upsert_entity("service:orphan", "Orphan", "service")
    assert _tools(store, catalog)["graph_path"].run(a="proj-a", b="Orphan").startswith("NO_PATH")
    assert _tools(store, catalog)["graph_path"].run(a="proj-a", b="warp").startswith("NO_RESULTS")


def test_graph_path_tool_default_hops_and_retry_hint(catalog, store):
    """Default max_hops is 5 (matches the web UI's Path Finder — the agent tool
    used to default to 3, clipping real chains one indirection layer deeper than
    that). A NO_PATH at a shallow max_hops nudges the model to retry wider
    before it's allowed to conclude the things are unrelated."""
    for eid, name, type_ in (("repo:a", "a", "repo"), ("project:mid", "Mid", "project"),
                             ("service:mid2", "Mid2", "service"), ("service:b", "b", "service")):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("d1", [("repo:a", "part_of", "project:mid", "")])
    catalog.replace_doc_edges("d2", [("project:mid", "part_of", "service:mid2", "")])
    catalog.replace_doc_edges("d3", [("service:mid2", "depends_on", "service:b", "")])

    tool = _tools(store, catalog)["graph_path"]
    out_default = tool.run(a="a", b="b")  # no max_hops passed -> should use the new default of 5
    assert "3 hop(s)" in out_default and "--depends_on-->" in out_default

    out_shallow = tool.run(a="a", b="b", max_hops=2)
    assert out_shallow.startswith("NO_PATH")
    assert "higher max_hops" in out_shallow


# ---------------------------------------------- graph_path_candidates (plan 06 §A)

def _seed_ambiguous_shape(catalog):
    """The §0 motivating shape at fixture scale: a 1-hop depends_on evidenced only by
    a meeting-notes page ("Jan 6, 2026"), plus a 2-hop chain through Stevedore
    evidenced by an AGENTS.md doc — materially different routes between the same two
    services."""
    for eid, name, type_ in (("service:connector", "Connector", "service"),
                             ("service:nautical", "Nautical", "service"),
                             ("service:stevedore", "Stevedore", "service")):
        catalog.upsert_entity(eid, name, type_)
    catalog.upsert_document("notes", "confluence:wiki", "https://wiki/x/jan6",
                            "Jan 6, 2026", "doc", "h1", None, 1)
    catalog.upsert_document("agents", "git:connector", "file:///repo/AGENTS.md",
                            "Connector/AGENTS.md", "doc", "h2", None, 1)
    catalog.replace_doc_edges("notes", [
        ("service:connector", "depends_on", "service:nautical", "usage events"),
    ])
    catalog.replace_doc_edges("agents", [
        ("service:connector", "depends_on", "service:stevedore", ""),
        ("service:nautical", "depends_on", "service:stevedore", ""),
    ])


def test_graph_path_candidates_matches_graph_path_when_single_chain(catalog):
    # reuse the linear fixture shape from test_catalog_graph_path_bfs
    for eid, name, type_ in (("repo:a", "a", "repo"), ("repo:b", "b", "repo"),
                             ("package:x", "X", "package"), ("ticket:t-1", "T-1", "ticket")):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("d1", [("repo:a", "depends_on", "package:x", "")])
    catalog.replace_doc_edges("d2", [("repo:b", "provides", "package:x", "")])
    catalog.replace_doc_edges("d3", [("repo:b", "references", "ticket:t-1", "")])

    single = catalog.graph_path("repo:a", "ticket:t-1")
    candidates = catalog.graph_path_candidates("repo:a", "ticket:t-1")
    assert candidates == [single]  # §2.2: candidate 0 agrees with graph_path
    assert catalog.graph_path_candidates("repo:a", "repo:a") == []
    assert catalog.graph_path_candidates("repo:a", "service:unconnected") == []


def test_graph_path_candidates_surfaces_materially_different_chains(catalog):
    _seed_ambiguous_shape(catalog)
    chains = catalog.graph_path_candidates("service:connector", "service:nautical")
    assert len(chains) == 2
    assert len(chains[0]) == 1 and chains[0][0]["evidence_title"] == "Jan 6, 2026"
    assert len(chains[1]) == 2  # through Stevedore, AGENTS.md-evidenced
    assert {h["evidence_title"] for h in chains[1]} == {"Connector/AGENTS.md"}


def test_graph_path_candidates_dedupes_same_signature_chains(catalog):
    """The same route re-evidenced by a second (same-class) doc is a duplicate, not
    a second answer."""
    catalog.upsert_entity("repo:a", "a", "repo")
    catalog.upsert_entity("package:x", "X", "package")
    catalog.replace_doc_edges("g1", [("repo:a", "depends_on", "package:x", "")])
    catalog.replace_doc_edges("g2", [("repo:a", "depends_on", "package:x", "")])
    chains = catalog.graph_path_candidates("repo:a", "package:x")
    assert len(chains) == 1


def test_graph_path_candidates_honors_hop_cap_and_max_candidates(catalog):
    _seed_ambiguous_shape(catalog)
    # hop cap 1: only the direct meeting-notes edge fits
    shallow = catalog.graph_path_candidates("service:connector", "service:nautical", max_hops=1)
    assert len(shallow) == 1 and len(shallow[0]) == 1
    # max_candidates=1: only the shortest distinct chain returned
    capped = catalog.graph_path_candidates("service:connector", "service:nautical",
                                           max_candidates=1)
    assert len(capped) == 1 and len(capped[0]) == 1


def test_graph_path_tool_single_healthy_chain_output_is_unchanged(catalog, store):
    """§2.5 golden: one healthy chain must render byte-for-byte today's format."""
    for eid, name, type_ in (("repo:a", "a", "repo"), ("repo:b", "b", "repo"),
                             ("package:x", "X", "package"), ("ticket:t-1", "T-1", "ticket")):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("d1", [("repo:a", "depends_on", "package:x", "")])
    catalog.replace_doc_edges("d2", [("repo:b", "provides", "package:x", "")])
    catalog.replace_doc_edges("d3", [("repo:b", "references", "ticket:t-1", "")])

    out = _tools(store, catalog)["graph_path"].run(a="a", b="T-1")
    assert out == (
        "Path from a to T-1 (3 hop(s)):\n"
        "1. a --depends_on--> X [evidence: d1]\n"
        "2. b --provides--> X [evidence: d2]\n"
        "3. b --references--> T-1 [evidence: d3]\n"
        "Cite the evidence documents for each hop you rely on."
    )


def test_graph_path_tool_flags_two_materially_different_chains(catalog, store):
    _seed_ambiguous_shape(catalog)
    out = _tools(store, catalog)["graph_path"].run(a="Connector", b="Nautical")
    assert "2 distinct recorded connections exist between Connector and Nautical" in out
    assert "Chain 1 (1 hop(s)" in out and "Chain 2 (2 hop(s)" in out
    assert "Jan 6, 2026" in out and "Connector/AGENTS.md" in out
    assert "do NOT present only one as the answer" in out


def test_edge_corroboration_counts_docs_and_sources(catalog):
    """One edge asserted by 3 docs across 2 sources: doc_count=3, source_count=2."""
    catalog.upsert_entity("service:a", "A", "service")
    catalog.upsert_entity("service:b", "B", "service")
    for doc_id, src in (("c1", "git:repo"), ("c2", "git:repo"), ("c3", "confluence:wiki")):
        catalog.upsert_document(doc_id, src, f"u-{doc_id}", f"Doc {doc_id}", "doc", "h", None, 1)
        catalog.replace_doc_edges(doc_id, [("service:a", "depends_on", "service:b", "")])
    assert catalog.edge_corroboration("service:a", "depends_on", "service:b") == {
        "doc_count": 3, "source_count": 2}
    assert catalog.edge_corroboration("service:a", "depends_on", "service:zzz") == {
        "doc_count": 0, "source_count": 0}


def test_graph_path_tool_annotates_chain_confidence_and_weak_hops(catalog, store):
    """§0 fixture: the meeting-notes chain carries its low score + caveat; the
    AGENTS.md chain scores higher — the ordering is stated, not implied."""
    _seed_ambiguous_shape(catalog)
    ledger: dict[str, float] = {}
    pipe = IngestPipeline(store, catalog)
    tools = {t.spec.name: t for t in build_builtin_tools(
        store, catalog, pipe, RetrievalConfig(), score_ledger=ledger)}
    out = tools["graph_path"].run(a="Connector", b="Nautical")
    assert "Chain 1 (1 hop(s), confidence 0.25):" in out
    assert "Chain 2 (2 hop(s), confidence 0.55):" in out
    assert 'low-confidence (0.25): sourced only from informal meeting notes ("Jan 6, 2026")' in out
    # score ledger (Phase C prep): server-side numbers recorded per evidence ref
    assert ledger["jan 6, 2026"] == 0.25
    assert ledger["connector/agents.md"] == 0.55


def test_graph_neighbors_flags_meeting_notes_edges(catalog, store):
    _seed_ambiguous_shape(catalog)
    out = _graph_tool(store, catalog).run(entity="Connector")
    assert "(low-confidence: meeting-notes evidence)" in out
    # only the meeting-notes edge is flagged, not the AGENTS.md one
    agents_line = next(l for l in out.splitlines() if "Stevedore" in l and "Connector " in l)
    assert "low-confidence" not in agents_line


def test_graph_neighbors_tool_formats_relationships(tmp_path, workspace, catalog, store):
    _write(tmp_path / "a", "src/Api/Api.csproj", CSPROJ_A)
    connector = FilesConnector(name="proj-a", options={"path": str(tmp_path / "a")},
                               workspace=workspace)
    IngestPipeline(store, catalog).ingest(connector.sync({}), "files:proj-a")

    out = _graph_tool(store, catalog).run(entity="nautical models")
    assert "--depends_on-->" in out and "AppRiver.Nautical.Models" in out
    assert "evidence:" in out and "dependencies & packages" in out


def test_graph_neighbors_tool_refuses_unknown(catalog, store):
    out = _graph_tool(store, catalog).run(entity="warp drive")
    assert out.startswith("NO_RESULTS")


def test_graph_neighbors_tool_caps_hub_entities(catalog, store):
    """A hub entity (a repo with thousands of relationships, in production) must
    never dump every row into one tool result — that's an unbounded LLM payload,
    and was observed to make the actual provider request fail outright rather
    than degrade gracefully. Past the threshold, output groups by relation type
    with a bounded sample per group instead of listing exhaustively."""
    catalog.upsert_entity("repo:hub", "Hub", "repo")
    for i in range(70):
        catalog.upsert_entity(f"symbol:s{i}", f"Symbol{i}", "symbol")
        catalog.replace_doc_edges(f"doc-{i}", [("repo:hub", "defines", f"symbol:s{i}", "")])
    for i in range(5):
        catalog.upsert_entity(f"package:p{i}", f"Package{i}", "package")
        catalog.replace_doc_edges(f"pkgdoc-{i}", [("repo:hub", "depends_on", f"package:p{i}", "")])

    out = _graph_tool(store, catalog).run(entity="Hub")
    assert "75 relationships" in out
    assert "too many to list in full" in out
    assert "graph_path" in out  # steers toward the bounded two-entity tool
    # Groups are labelled by relation AND direction (`--rel-->` outgoing, `<--rel--`
    # incoming), so a hub's incoming edges get their own sample budget instead of being
    # sorted out of a shared one — see test_tables.py for the regression that forced it.
    assert "--defines--> (70 total)" in out
    assert "…and 62 more not shown" in out  # 70 - 8 sampled
    assert "--depends_on--> (5 total)" in out
    # 8 sampled rows + the group header line; 70 are never all listed.
    assert out.count("--defines-->") == 9
    assert out.count("--depends_on-->") == 6  # 5 rows + header: under the cap, shown in full


def test_graph_neighbors_tool_lists_in_full_under_threshold(catalog, store):
    """The common case (a modest number of relationships) is untouched: no
    grouping, no sampling, every row shown — exactly as before this fix."""
    catalog.upsert_entity("repo:small", "Small", "repo")
    for i in range(3):
        catalog.upsert_entity(f"package:q{i}", f"Q{i}", "package")
        catalog.replace_doc_edges(f"d-{i}", [("repo:small", "depends_on", f"package:q{i}", "")])

    out = _graph_tool(store, catalog).run(entity="Small")
    assert "3 relationship(s):" in out
    assert "too many to list" not in out
    assert out.count("--depends_on-->") == 3


# ------------------------------------------------------- graph-expansion retrieval

def _seed_two_linked_docs(catalog):
    # d1 and d2 both define symbols in repo:x, so they are one graph hop apart; d3 is
    # in a different repo and shares nothing with them.
    for did, uri, title in (("d1", "a.py", "Module A"), ("d2", "b.py", "Payments Module"),
                            ("d3", "c.py", "Unrelated")):
        catalog.upsert_document(did, "git:x", uri, title, "code", f"h-{did}", None, 1)
    for eid, name, type_ in (("repo:x", "x", "repo"), ("repo:z", "z", "repo"),
                             ("symbol:a", "A", "symbol"), ("symbol:b", "B", "symbol"),
                             ("symbol:c", "C", "symbol")):
        catalog.upsert_entity(eid, name, type_)
    catalog.replace_doc_edges("d1", [("repo:x", "defines", "symbol:a", "in a.py")])
    catalog.replace_doc_edges("d2", [("repo:x", "defines", "symbol:b", "in b.py")])
    catalog.replace_doc_edges("d3", [("repo:z", "defines", "symbol:c", "in c.py")])


def test_graph_expand_finds_linked_docs_only(catalog):
    _seed_two_linked_docs(catalog)
    related = catalog.graph_expand(["d1"])
    assert {r["doc_id"] for r in related} == {"d2"}  # shares repo:x; d3 is disconnected
    assert related[0]["title"] == "Payments Module"
    assert catalog.graph_expand([]) == []
    assert catalog.graph_expand(["unknown"]) == []


def test_search_memory_appends_graph_expansion(store, catalog):
    _seed_two_linked_docs(catalog)
    # give d1 real chunk text so the query grounds on it (d2 does NOT match the query)
    store.upsert_document("d1", "git:x", "a.py", "Module A", "code", ["alpha beta gamma settle"])
    tools = _tools(store, catalog)
    out = tools["search_memory"].run(query="alpha beta gamma settle")
    assert "Module A" in out and "score:" in out          # primary grounded hit
    assert "RELATED via knowledge graph" in out
    assert "Payments Module" in out                        # d2 surfaced via the graph


def test_search_memory_no_expansion_without_graph(store, catalog):
    store.upsert_document("d9", "wiki:x", "u", "Lonely", "doc", ["alpha beta gamma"])
    out = _tools(store, catalog)["search_memory"].run(query="alpha beta gamma")
    assert "RELATED via knowledge graph" not in out  # no edges -> no expansion section


def test_end_to_end_contextual_codegraph_expansion(store, catalog):
    """Whole correlation stack through the real pipeline: two code files ingested with
    contextual chunking auto-create repo->defines edges; a query grounds on one file and
    the sibling file surfaces via the shared repo node in the graph-expansion section."""
    pipe = IngestPipeline(store, catalog, RetrievalConfig())  # contextual chunking on
    pipe.ingest(
        [
            Document(uri="src/pay.py", title="pay.py",
                     text="import stripe\nclass PaymentProcessor:\n    def settle(self):\n"
                          "        return zulu_unique_marker\n", kind="code"),
            Document(uri="src/ledger.py", title="ledger.py",
                     text="import stripe\nclass Ledger:\n    def record(self):\n        pass\n",
                     kind="code"),
        ],
        "git:platform",
    )
    tools = {
        t.spec.name: t
        for t in build_builtin_tools(store, catalog, pipe, RetrievalConfig(min_score=0.1), None)
    }
    out = tools["search_memory"].run(query="zulu_unique_marker")
    assert "git:platform · pay.py" in out           # contextual breadcrumb on the grounded hit
    assert "RELATED via knowledge graph" in out
    assert "ledger.py" in out                        # sibling surfaced via shared repo node
