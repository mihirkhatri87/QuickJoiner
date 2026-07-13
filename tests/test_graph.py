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
