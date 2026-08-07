"""Per-source breakdown — showing what each system separately asserts.

The motivating report: "list all teams and their team members" is answerable from an
internal catalogue, from Confluence charters and from TFS, and the user got one merged
answer with no sign of the alternatives. The knowledge graph returns a **union**, so the
per-system picture was invisible by construction.

These tests pin the corrections that measuring against the real 11-connector corpus forced,
each of which had made the first implementation over-claim:

  * a naming variant (`AppRiver\\SecureCloud 2.0\\Caffeine` vs `Caffeine`) is NOT a
    difference — comparing raw names reported systems as conflicting when they agreed;
  * systems listing the same members must stay silent, or real splits drown in noise;
  * the retrieval side announces nothing at all, because a threshold sweep found no setting
    that separates "three systems each answer this" from "one answer built from three" —
    only the invisible score ledger runs there.
"""

from __future__ import annotations

from dataclasses import dataclass

from quickjoiner.agent.divergence import (
    fold_member,
    ledger_entries_for_hits,
    membership_by_source,
    render_membership_split,
    source_label,
)


@dataclass
class Hit:
    """The fields the ledger reads off a memory.store.SearchHit."""

    score: float
    source_id: str
    doc_id: str
    title: str = "Some page"
    uri: str = "https://x/p"
    kind: str = "doc"


def _edge(src_name: str, source_id: str) -> dict:
    """One `graph_relations` row, as `catalog._read_all` returns it."""
    return {
        "src": f"person:{src_name.lower()}", "src_name": src_name,
        "dst": "team:caffeine", "dst_name": "Caffeine", "rel": "works_on",
        "evidence_source_id": source_id, "evidence_title": "t", "evidence_uri": "u",
    }


# ------------------------------------------------------------- name folding

def test_a_naming_convention_is_not_a_disagreement():
    """Measured on the real graph: three of the top "contested" groups were one team
    spelled two ways — TFS qualifies by backlog path, the catalogue does not."""
    assert fold_member("AppRiver\\SecureCloud 2.0\\Caffeine") == "caffeine"
    assert fold_member("Caffeine") == "caffeine"
    assert fold_member("AppRiver/SecureCloud 2.0/PSMQ/Narwhals") == "narwhals"
    # It must not merge genuinely different names.
    assert fold_member("Black Team") != fold_member("SecureTide")


def test_systems_agreeing_through_different_spellings_stay_silent():
    rows = [
        _edge("Caffeine", "web_scrape:Plumber"),
        _edge("AppRiver\\SecureCloud 2.0\\Caffeine", "azure_devops:TFS Appriver"),
    ]
    assert membership_by_source(rows) == []


# ------------------------------------------------------- the per-source split

def test_systems_listing_different_members_are_broken_down():
    """The real shape: the charter names a Delivery Manager the HR catalogue does not
    carry, and the catalogue names a developer the charter has not been updated with."""
    rows = [
        _edge("Liu Maumasi", "web_scrape:Plumber"),
        _edge("Mihir Khatri", "web_scrape:Plumber"),
        _edge("Liu Maumasi", "confluence:Appriver Confluence"),
        _edge("Etheria Hill", "confluence:Appriver Confluence"),
    ]
    assert membership_by_source(rows) == [
        ("Appriver Confluence", ["Etheria Hill", "Liu Maumasi"]),
        ("Plumber", ["Liu Maumasi", "Mihir Khatri"]),
    ]


def test_identical_membership_is_agreement_and_stays_silent():
    rows = [
        _edge("Liu Maumasi", "web_scrape:Plumber"),
        _edge("Liu Maumasi", "confluence:Appriver Confluence"),
    ]
    assert membership_by_source(rows) == []


def test_a_single_system_has_nothing_to_compare():
    rows = [_edge("Liu Maumasi", "web_scrape:Plumber"),
            _edge("Mihir Khatri", "web_scrape:Plumber")]
    assert membership_by_source(rows) == []


def test_an_edge_with_no_evidence_source_is_skipped_not_a_phantom_system():
    """A same_as bridge cites no document. It must not become a third system."""
    rows = [
        _edge("Liu Maumasi", "web_scrape:Plumber"),
        _edge("Mihir Khatri", "web_scrape:Plumber"),
        _edge("Ghost", ""),
    ]
    assert membership_by_source(rows) == []


def test_the_rendering_never_claims_the_systems_are_wrong():
    """A difference is as likely to be partial coverage as contradiction, and nothing here
    can tell which — so the wording must not assert one."""
    lines = render_membership_split("Caffeine", [("Plumber", ["Liu Maumasi"])])
    text = "\n".join(lines).lower()
    assert "per plumber: liu maumasi" in text
    assert "disagree" not in text and "conflict" not in text and "wrong" not in text
    assert render_membership_split("Caffeine", []) == []


def test_source_label_strips_the_connector_type_prefix():
    assert source_label("web_scrape:Plumber") == "Plumber"
    assert source_label("confluence:Appriver Confluence") == "Appriver Confluence"


# ----------------------------------------------------------- the score ledger

def test_retrieved_sources_get_a_server_side_score_so_candidates_are_not_unscored():
    """Before this, only graph_path populated the ledger, so any candidate drawn from
    memory rendered "unscored" — which is what limited the carousel to graph questions."""
    hits = [
        Hit(0.81, "web_scrape:Plumber", "d1", title="Caffeine"),
        Hit(0.74, "confluence:Appriver Confluence", "d2", title="Caffeine - Team Charter"),
    ]
    ledger = ledger_entries_for_hits(hits)
    # Keyed by the lowercased citation label — what candidates.attach_confidence matches.
    assert ledger["caffeine"] > 0
    assert ledger["caffeine - team charter"] > 0
    assert all(0.05 <= v <= 0.95 for v in ledger.values())


def test_the_ledger_is_silent_on_an_empty_search():
    assert ledger_entries_for_hits([]) == {}


def test_agreement_across_sources_scores_higher_than_a_lone_source():
    """Reuses confidence.score_edge's existing model rather than a second scale: two
    systems carrying the same question is corroboration."""
    lone = ledger_entries_for_hits([Hit(0.8, "web_scrape:Plumber", "d1", title="X")])
    both = ledger_entries_for_hits([
        Hit(0.8, "web_scrape:Plumber", "d1", title="X"),
        Hit(0.8, "confluence:Appriver Confluence", "d2", title="Y"),
    ])
    assert both["x"] > lone["x"]


# ------------------------------------------------------------- end to end

def _seed_split(catalog):
    for eid, name, type_ in (
        ("team:caffeine", "Caffeine", "team"),
        ("person:liu maumasi", "Liu Maumasi", "person"),
        ("person:mihir khatri", "Mihir Khatri", "person"),
        ("person:etheria hill", "Etheria Hill", "person"),
    ):
        catalog.upsert_entity(eid, name, type_, "web_scrape:Plumber")
    catalog.upsert_document("dp", "web_scrape:Plumber",
                            "https://plumber/TeamDetails?team=Caffeine", "Caffeine",
                            "web", "h1", "2026-08-05", 1)
    catalog.upsert_document("dc", "confluence:Appriver Confluence",
                            "https://wiki/Caffeine+Team+Charter", "Caffeine - Team Charter",
                            "doc", "h2", "2026-08-05", 1)
    catalog.replace_doc_edges("dp", [
        ("person:liu maumasi", "works_on", "team:caffeine", "table"),
        ("person:mihir khatri", "works_on", "team:caffeine", "table"),
    ])
    catalog.replace_doc_edges("dc", [
        ("person:liu maumasi", "works_on", "team:caffeine", "charter"),
        ("person:etheria hill", "works_on", "team:caffeine", "charter"),
    ])


def test_graph_relations_shows_each_system_instead_of_only_the_union(catalog, store):
    """The user's actual question. Before this the tool printed the 3-name union and
    nothing revealed that neither system asserts all three."""
    from quickjoiner.agent.tools import build_builtin_tools
    from quickjoiner.config import RetrievalConfig

    _seed_split(catalog)
    tools = {t.spec.name: t for t in build_builtin_tools(store, catalog, None, RetrievalConfig())}
    out = tools["graph_relations"].fn(rel="works_on", src_type="person", dst_type="team")

    assert "per Plumber: Liu Maumasi, Mihir Khatri" in out
    assert "per Appriver Confluence: Etheria Hill, Liu Maumasi" in out
    # The union is still shown — it is a real fact — but not offered as anyone's answer.
    assert "no single system asserts" in out
    # And it must not accuse either system of being wrong.
    assert "disagree, or simply that each covers a different part" in out


def test_graph_relations_stays_quiet_when_the_systems_match(catalog, store):
    """The common case must read exactly as it did before the feature existed."""
    from quickjoiner.agent.tools import build_builtin_tools
    from quickjoiner.config import RetrievalConfig

    for eid, name, type_ in (("team:caffeine", "Caffeine", "team"),
                             ("person:liu maumasi", "Liu Maumasi", "person")):
        catalog.upsert_entity(eid, name, type_, "web_scrape:Plumber")
    catalog.upsert_document("dp", "web_scrape:Plumber", "https://plumber/T?team=Caffeine",
                            "Caffeine", "web", "h1", "2026-08-05", 1)
    catalog.upsert_document("dc", "confluence:Appriver Confluence", "https://wiki/C",
                            "Charter", "doc", "h2", "2026-08-05", 1)
    catalog.replace_doc_edges("dp", [("person:liu maumasi", "works_on", "team:caffeine", "")])
    catalog.replace_doc_edges("dc", [("person:liu maumasi", "works_on", "team:caffeine", "")])

    tools = {t.spec.name: t for t in build_builtin_tools(store, catalog, None, RetrievalConfig())}
    out = tools["graph_relations"].fn(rel="works_on", src_type="person", dst_type="team")
    assert "    - per " not in out
    assert "per-source breakdown" not in out


def test_search_memory_announces_nothing_about_provenance(catalog, store):
    """Measured: a retrieval-side "several systems answer this" rule fired on 80% of real
    questions and no threshold separated the classes. The chunk labels already carry the
    provenance, so the section was removed rather than tuned."""
    from quickjoiner.agent.tools import build_builtin_tools
    from quickjoiner.config import RetrievalConfig
    from quickjoiner.connectors.base import Document
    from quickjoiner.ingest.pipeline import IngestPipeline

    pipe = IngestPipeline(store, catalog)
    for sid, uri in (("web_scrape:Plumber", "https://p/1"), ("confluence:C", "https://c/1")):
        pipe.ingest(
            [Document(uri=uri, title="Caffeine roster", text="Liu Maumasi is on Caffeine.")],
            sid,
        )

    ledger: dict[str, float] = {}
    tools = {t.spec.name: t for t in build_builtin_tools(
        store, catalog, pipe, RetrievalConfig(min_score=0.0), None, ledger)}
    out = tools["search_memory"].fn(query="who is on Caffeine")

    assert "INDEPENDENT SOURCES" not in out
    assert "[source:" in out  # provenance is already on every chunk
    # ...but the invisible half still ran, so a candidate could be scored.
    assert ledger
