"""Cross-source identity bridges (ingest/bridges.py + catalog wiring, AI roadmap #24).

The measured failure this fixes: entities are keyed `type:name` and resolution is
same-type-only, so `service:connector` / `repo:connector` / the pipeline that builds it
were three islands — 158 exact service↔repo name matches on the live workspace, but only
18 multi-source entities. Bridges make `graph_path` and `graph_expand` cross sources
without ever weakening the grounding gate or claiming document evidence they don't have.
"""

from __future__ import annotations

from quickjoiner.agent.confidence import score_chain, score_edge
from quickjoiner.ingest.bridges import compute_same_as_bridges, normalize_name


def _e(id_, name, type_):
    return {"id": id_, "name": name, "type": type_}


# ---------------------------------------------------------------- pure function

def test_cross_type_same_name_is_bridged():
    bridges = compute_same_as_bridges([
        _e("service:connector", "Connector", "service"),
        _e("repo:connector", "connector", "repo"),
    ])
    assert len(bridges) == 1
    src, rel, dst, detail = bridges[0]
    assert rel == "same_as" and {src, dst} == {"service:connector", "repo:connector"}
    assert "no document asserts this identity" in detail


def test_normalization_collapses_spoken_forms():
    # AppRiver.Connector / appriver-connector / AppRiver Connector all agree.
    assert normalize_name("AppRiver.Connector") == normalize_name("appriver-connector") \
        == normalize_name("AppRiver Connector")
    bridges = compute_same_as_bridges([
        _e("repo:appriver.connector", "AppRiver.Connector", "repo"),
        _e("pipeline:appriver-connector", "appriver-connector", "pipeline"),
    ])
    assert len(bridges) == 1


def test_same_type_pairs_are_never_bridged():
    # Same-type dedup is entity resolution's job — a bridge would mask a merge failure.
    assert compute_same_as_bridges([
        _e("repo:a", "Stevedore", "repo"),
        _e("repo:b", "Stevedore", "repo"),
    ]) == []


def test_short_and_generic_names_are_skipped():
    assert compute_same_as_bridges([
        _e("service:api", "API", "service"), _e("repo:api", "api", "repo"),  # < 5 chars
    ]) == []
    assert compute_same_as_bridges([
        _e("service:coreapi", "Core API", "service"),  # every token generic
        _e("repo:core-api", "core-api", "repo"),
    ]) == []


def test_oversized_name_groups_are_treated_as_generic():
    # A "name" shared by 7 entities is not an identity — skip the whole group.
    members = [_e(f"repo:mgmt{i}", "Management Console", "repo") for i in range(4)]
    members += [_e(f"service:mgmt{i}", "Management Console", "service") for i in range(3)]
    assert compute_same_as_bridges(members) == []


def test_types_outside_the_identity_cluster_are_ignored():
    assert compute_same_as_bridges([
        _e("ticket:conn-1", "Connector", "ticket"),
        _e("repo:connector", "Connector", "repo"),
    ]) == []


def test_declared_alias_bridges_what_names_never_could():
    # Repo "Stevedore" aka "appriver.provisioning" ⇔ the Octopus service of that name —
    # no name match exists, only the human-declared alias.
    bridges = compute_same_as_bridges(
        [
            _e("repo:stevedore", "Stevedore", "repo"),
            _e("service:appriver.provisioning", "AppRiver.Provisioning", "service"),
        ],
        aliases=[("repo:stevedore", "appriver.provisioning")],
    )
    assert len(bridges) == 1
    src, _, dst, detail = bridges[0]
    assert {src, dst} == {"repo:stevedore", "service:appriver.provisioning"}
    assert "via declared alias 'appriver.provisioning'" in detail


def test_alias_matches_obey_the_same_guards_and_never_duplicate():
    # A generic alias asserts nothing; and a pair matching by both name AND alias is one bridge.
    assert compute_same_as_bridges(
        [_e("repo:x", "Stevedore", "repo"), _e("service:y", "Core API", "service")],
        aliases=[("repo:x", "core api")],
    ) == []
    both = compute_same_as_bridges(
        [_e("repo:connector", "Connector", "repo"), _e("service:connector", "Connector", "service")],
        aliases=[("repo:connector", "connector")],
    )
    assert len(both) == 1


# ---------------------------------------------------------------- confidence

def test_bridge_hop_scores_as_its_own_low_class_and_caps_the_chain():
    # No corroboration is possible (no evidence doc) — base 0.30, nothing added.
    assert score_edge("name-bridge", 0, 0) == 0.30
    # min-rule: any chain crossing a bridge is capped at the bridge's score.
    assert score_chain([0.60, 0.30, 0.60]) == 0.30


# ---------------------------------------------------------------- catalog wiring

def _seed_cross_source_world(catalog):
    """git doc proves repo:connector defines a symbol; octopus doc proves
    service:connector deploys to production. No shared entity — only a bridge links them."""
    catalog.upsert_document("d-git", "git:Connector", "u::a.cs", "a.cs", "code", "h1", "2026-07-01", 1)
    catalog.upsert_document("d-oct", "octopus:Appriver", "oct://dash", "Deploy dashboard", "deployment", "h2", "2026-07-01", 1)
    catalog.upsert_entity("repo:connector", "Connector", "repo", "git:Connector")
    catalog.upsert_entity("symbol:mailer", "Mailer", "symbol", "git:Connector")
    catalog.upsert_entity("service:connector", "Connector", "service", "octopus:Appriver")
    catalog.upsert_entity("environment:production", "Production", "environment", "octopus:Appriver")
    catalog.replace_doc_edges("d-git", [("repo:connector", "defines", "symbol:mailer", "")])
    catalog.replace_doc_edges("d-oct", [("service:connector", "deploys", "environment:production", "")])


def test_refresh_bridges_and_graph_path_crosses_sources(catalog):
    _seed_cross_source_world(catalog)
    assert catalog.graph_path("repo:connector", "environment:production", max_hops=3) is None  # islands

    assert catalog.refresh_same_as_bridges() == 1
    path = catalog.graph_path("repo:connector", "environment:production", max_hops=3)
    assert path is not None and [h["rel"] for h in path] == ["same_as", "deploys"]

    # Idempotent: recomputing neither duplicates nor drifts.
    assert catalog.refresh_same_as_bridges() == 1
    assert len([e for e in catalog.graph_snapshot(limit=100)["edges"] if e["rel"] == "same_as"]) == 1


def test_graph_expand_crosses_a_bridge_to_real_evidence_only(catalog):
    _seed_cross_source_world(catalog)
    assert catalog.graph_expand(["d-git"]) == []  # islands: nothing beyond the seed doc
    catalog.refresh_same_as_bridges()
    rows = catalog.graph_expand(["d-git"])
    # The octopus doc surfaces — via the bridge — but the returned row is the REAL
    # deploys edge with its citable document, never the bridge itself.
    assert any(r["doc_id"] == "d-oct" and r["rel"] == "deploys" for r in rows)
    assert all(r["rel"] != "same_as" for r in rows)


def test_aka_option_flows_from_source_registration_to_a_bridge(catalog):
    """The connector's "also known as" field end-to-end: upsert_source persists the
    aliases on the source entity (created eagerly so they resolve immediately), and the
    bridge refresh links the repo to the same-named service across sources."""
    catalog.upsert_source("git:Stevedore", "Stevedore", "git",
                          {"url": "u", "aka": "appriver.provisioning, provisioner"})
    assert catalog.resolve_entity("appriver.provisioning")["id"] == "repo:stevedore"

    catalog.upsert_document("d-oct", "octopus:Appriver", "oct://dash", "Dash", "deployment", "h", "2026-07-01", 1)
    catalog.upsert_entity("service:appriver.provisioning", "AppRiver.Provisioning", "service", "octopus:Appriver")
    catalog.upsert_entity("environment:production", "Production", "environment", "octopus:Appriver")
    catalog.replace_doc_edges("d-oct", [("service:appriver.provisioning", "deploys", "environment:production", "")])

    assert catalog.refresh_same_as_bridges() == 1
    path = catalog.graph_path("repo:stevedore", "environment:production", max_hops=3)
    assert path is not None and [h["rel"] for h in path] == ["same_as", "deploys"]

    # A list-valued aka (the web form sends arrays) works identically.
    catalog.upsert_source("git:Other", "Other", "git", {"aka": ["provisioning-worker"]})
    assert catalog.resolve_entity("provisioning-worker")["id"] == "repo:other"


def test_gc_ignores_bridges_and_sweeps_dangling_ones(catalog):
    _seed_cross_source_world(catalog)
    catalog.refresh_same_as_bridges()
    # Deleting the octopus doc orphans service:connector + environment:production —
    # the bridge alone must NOT keep the service alive, and must not dangle after gc.
    catalog.delete_document("d-oct")
    removed = catalog.gc_orphan_entities()
    assert removed == 2
    snapshot = catalog.graph_snapshot(limit=100)
    assert all(e["rel"] != "same_as" for e in snapshot["edges"])  # dangling bridge swept
    assert {n["id"] for n in snapshot["nodes"]} == {"repo:connector", "symbol:mailer"}
