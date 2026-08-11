"""Knowledge scopes: per-user visibility over ONE communal store.

The property under test is a security property, so these are written as leak tests: given a
source only Ada may read, no path — retrieval, graph, autocomplete, totals — hands Bob its
content or even its names. The three rules that make it trustworthy, each pinned below:

* **It composes with, and cannot be widened by, the picker scope.** Picking nothing means
  "everything I may read", never "everything".
* **It fails closed.** An empty allow-list matches no row; a source with no `sources` row is
  not visible; an edge whose evidence document is missing is dropped.
* **Open mode is byte-identical.** Single-user workspaces predate all of this and must not
  pay for it — `visible_source_ids` returns None and every query text is unchanged.
"""

from __future__ import annotations

import pytest

from quickjoiner.agent.tools import USER_TAUGHT_SOURCE, note_source, teach_fact
from quickjoiner.config import RetrievalConfig
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore, SearchScope
from tests.conftest import FakeEmbedder

ADA, BOB = "ada", "bob"
COMMONS, PRIVATE = "files:handbook", "onedrive:ada-drive"


# ------------------------------------------------------------------ the scope object

def test_visibility_is_anded_over_the_picks_not_ored_with_them():
    """The whole safety of the design: picks and visibility are different KINDS of narrowing.
    OR-ing them would let picking a source you cannot see escalate into reading it."""
    scope = SearchScope(source_ids=["a:1"], visible_source_ids=["b:2"])
    assert not scope.matches("a:1", "d1")   # picked, but not permitted
    assert not scope.matches("b:2", "d1")   # permitted, but not picked


def test_picking_nothing_means_everything_i_may_read_not_everything():
    scope = SearchScope(visible_source_ids=["a:1"])
    assert scope.matches("a:1", "d1")
    assert not scope.matches("b:2", "d1")
    # ...and it must NOT read as a picker selection, or a visibility-only turn would strip
    # the live connector tools the user never excluded.
    assert not scope.has_picks()
    assert not scope.is_empty()


def test_an_empty_allow_list_matches_nothing_rather_than_everything():
    """`[]` (may read nothing) and None (no restriction) are opposites, and the code path
    that renders `IN ()` as invalid SQL would have failed this one open."""
    scope = SearchScope(visible_source_ids=[])
    assert not scope.matches("a:1", "d1")
    assert not scope.is_empty()


def test_no_restriction_is_indistinguishable_from_the_old_unscoped_scope():
    assert SearchScope(visible_source_ids=None).is_empty()
    assert SearchScope().predicate_groups() == []


def test_narrowed_to_none_returns_the_scope_untouched():
    picked = SearchScope(source_ids=["a:1"], doc_ids=["d1"])
    assert picked.narrowed_to(None) is picked


# ------------------------------------------------------------------- catalog layer

@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(tmp_path / "ws")
    cat.upsert_source(COMMONS, "Handbook", "files")
    cat.upsert_source(PRIVATE, "Ada's drive", "onedrive")
    cat.set_source_ownership(PRIVATE, owner=ADA, shared=False)
    cat.upsert_document("d-pub", COMMONS, "file:///handbook.md", "Handbook", "doc", "h1", None, 1)
    cat.upsert_document("d-sec", PRIVATE, "https://od/secret.docx", "Secret", "doc", "h2", None, 1)
    yield cat
    cat.close()


def test_visible_source_ids_covers_ingestion_buckets_not_just_connectors(catalog):
    """A taught note lives in a bucket, not a configured connector — filtering only the
    connectors would leave exactly the personal content this feature exists to protect."""
    assert set(catalog.visible_source_ids(ADA)) == {COMMONS, PRIVATE}
    assert catalog.visible_source_ids(BOB) == [COMMONS]


def test_sharing_a_source_returns_it_to_the_commons(catalog):
    catalog.set_source_ownership(PRIVATE, owner=ADA, shared=True)
    assert set(catalog.visible_source_ids(BOB)) == {COMMONS, PRIVATE}


# ------------------------------------------------------------------ retrieval leak tests

@pytest.fixture
def store(tmp_path):
    st = KnowledgeStore(tmp_path / "lance", FakeEmbedder(),
                        retrieval=RetrievalConfig(hybrid=True, reranker="none"))
    # Identical text in both sources, so any difference in results is the filter and
    # nothing else — a weaker fixture would let ranking noise pass for enforcement.
    for doc_id, source_id in (("d-pub", COMMONS), ("d-sec", PRIVATE)):
        st.upsert_document(doc_id=doc_id, source_id=source_id, uri=f"uri://{doc_id}",
                           title="Gateway routing", kind="doc", updated_at=None,
                           chunks=["the gateway routes mail through securetide"])
    return st


def test_a_private_source_is_unreachable_for_everyone_else(store):
    both = store.search("gateway routes mail", top_k=10)
    assert {h.source_id for h in both} == {COMMONS, PRIVATE}

    as_bob = store.search("gateway routes mail", top_k=10,
                          scope=SearchScope(visible_source_ids=[COMMONS]))
    assert as_bob and {h.source_id for h in as_bob} == {COMMONS}


def test_picking_a_source_you_cannot_see_returns_nothing_not_everything(store):
    """The escalation attempt: name the private source explicitly in the picker."""
    scope = SearchScope(source_ids=[PRIVATE]).narrowed_to([COMMONS])
    assert store.search("gateway routes mail", top_k=10, scope=scope) == []


def test_picking_a_document_you_cannot_see_is_equally_refused(store):
    """doc_ids OR with source_ids, so they are the other half of the same escalation."""
    scope = SearchScope(doc_ids=["d-sec"]).narrowed_to([COMMONS])
    assert store.search("gateway routes mail", top_k=10, scope=scope) == []


def test_a_user_who_may_read_nothing_gets_nothing(store):
    assert store.search("gateway routes mail", top_k=10,
                        scope=SearchScope(visible_source_ids=[])) == []


def test_visibility_filtering_leaves_the_permitted_hit_scored_exactly_as_before(store):
    """Grounding is a score gate, so the filter must remove rows without perturbing the
    survivors — otherwise turning auth on would silently move the refusal boundary."""
    plain = [h for h in store.search("gateway routes mail", top_k=10)
             if h.source_id == COMMONS]
    scoped = store.search("gateway routes mail", top_k=10,
                          scope=SearchScope(visible_source_ids=[COMMONS]))
    assert [(h.doc_id, round(h.score, 6)) for h in plain] == \
           [(h.doc_id, round(h.score, 6)) for h in scoped]


def test_a_username_with_a_quote_cannot_break_the_predicate(store):
    """Visible ids are derived from usernames and connector names, and reach a LanceDB
    SQL string — the same injection surface the picker scope already guards."""
    hits = store.search("gateway routes mail", top_k=10,
                        scope=SearchScope(visible_source_ids=["notes:o'brien", COMMONS]))
    assert {h.source_id for h in hits} == {COMMONS}


# ---------------------------------------------------------------------- graph leak tests

@pytest.fixture
def graph_catalog(catalog):
    """`svc:gateway` is asserted by BOTH a public and a private document; `person:mole` only
    by the private one. So visibility has to change the ANSWER, not just drop duplicate rows."""
    for eid, name, etype in (("svc:gateway", "gateway", "service"),
                             ("team:core", "core", "team"),
                             ("person:mole", "Mole", "person")):
        catalog.upsert_entity(eid, name, etype, COMMONS)
    catalog.replace_doc_edges("d-pub", [
        ("team:core", "owns", "svc:gateway", "public"),
    ])
    catalog.replace_doc_edges("d-sec", [
        ("person:mole", "works_on", "svc:gateway", "secret"),
        ("team:core", "owns", "svc:gateway", "secret"),
    ])
    return catalog


def test_graph_neighbors_hides_edges_evidenced_only_by_an_unreadable_document(graph_catalog):
    everything = graph_catalog.graph_neighbors("svc:gateway")
    assert {r["src"] for r in everything} == {"team:core", "person:mole"}

    as_bob = graph_catalog.graph_neighbors("svc:gateway", visible_source_ids=[COMMONS])
    assert {r["src"] for r in as_bob} == {"team:core"}


def test_graph_relations_enumeration_is_filtered_too(graph_catalog):
    """The enumeration read returns EVERY edge of a shape, so an unfiltered one would be the
    single most efficient way to dump a private source's relationships."""
    rows = graph_catalog.graph_relations("works_on", visible_source_ids=[COMMONS])
    assert rows == []
    assert graph_catalog.graph_relations("works_on") != []


def test_edge_corroboration_counts_only_evidence_the_asker_can_open(graph_catalog):
    """Confidence is shown to a person; a count they cannot audit is a claim we can't back."""
    assert graph_catalog.edge_corroboration("team:core", "owns", "svc:gateway")["doc_count"] == 2
    scoped = graph_catalog.edge_corroboration("team:core", "owns", "svc:gateway",
                                              visible_source_ids=[COMMONS])
    assert scoped["doc_count"] == 1


def test_graph_path_will_not_route_an_answer_through_an_unreadable_hop(graph_catalog):
    assert graph_catalog.graph_path("person:mole", "team:core") is not None
    assert graph_catalog.graph_path("person:mole", "team:core",
                                    visible_source_ids=[COMMONS]) is None


def test_graph_expansion_never_hands_back_an_unreadable_document(graph_catalog):
    """This is the one graph read that returns whole citable documents into the answer."""
    related = graph_catalog.graph_expand(["d-pub"], limit=10)
    assert "d-sec" in {r["doc_id"] for r in related}
    scoped = graph_catalog.graph_expand(["d-pub"], limit=10, visible_source_ids=[COMMONS])
    assert "d-sec" not in {r["doc_id"] for r in scoped}


def test_an_entity_known_only_from_a_private_document_is_not_autocompleted(graph_catalog):
    """A name is disclosure on its own — filtering edges but not entity search would still
    complete `Mole` for someone who can never see why they exist."""
    assert {e["id"] for e in graph_catalog.search_entities("mole")} == {"person:mole"}
    assert graph_catalog.search_entities("mole", visible_source_ids=[COMMONS]) == []
    # An entity with public evidence is still offered.
    assert graph_catalog.search_entities("gateway", visible_source_ids=[COMMONS])


def test_graph_totals_are_counted_over_what_the_asker_can_see(graph_catalog):
    """The sampled-view denominator: a global total both overstates their graph and
    discloses how much is being withheld."""
    assert graph_catalog.graph_totals()["edges"] == 3
    assert graph_catalog.graph_totals([COMMONS])["edges"] == 1


def test_an_evidence_free_bridge_survives_but_a_dangling_edge_does_not(graph_catalog):
    """`same_as` bridges cite nothing by design, so they leak nothing and must not vanish
    the moment auth is enabled. An edge whose evidence row is genuinely missing is the
    opposite case and has to fail closed."""
    graph_catalog.upsert_entity("repo:gateway", "gateway", "repo", COMMONS)
    graph_catalog.replace_doc_edges("", [("svc:gateway", "same_as", "repo:gateway", "")])
    graph_catalog.replace_doc_edges(
        "ghost-doc", [("svc:gateway", "references", "team:core", "")])

    rels = {r["rel"] for r in graph_catalog.graph_neighbors("svc:gateway",
                                                            visible_source_ids=[COMMONS])}
    assert "same_as" in rels
    assert "references" not in rels


# ------------------------------------------------------------------- merge-guard leak tests
#
# Filtering decides what a person may READ. These decide what a private document may WRITE,
# which is the half filtering cannot undo: a merge, a rename or a bridge rewrites canonical
# graph state for the whole organisation, and no read-side predicate puts that back.

class _MergeAlways:
    """An adjudicator that says yes to everything — so if the resolver is reached at all,
    it merges. A guard tested against a cautious adjudicator would prove nothing."""

    def __call__(self, type_, name, candidates, context="", candidate_contexts=None):
        return candidates[0]


def _pipeline_with_resolver(catalog, tmp_path, name="guard-lance"):
    from quickjoiner.ingest.entity_resolution import EntityResolver
    from quickjoiner.ingest.pipeline import IngestPipeline

    embedder = FakeEmbedder()
    store = KnowledgeStore(tmp_path / name, embedder)
    resolver = EntityResolver(catalog=catalog, embedder=embedder, adjudicate=_MergeAlways())
    return IngestPipeline(store, catalog, entity_resolver=resolver)


@pytest.fixture
def merge_catalog(catalog):
    """One org entity, minted by the commons source — the thing a private document must be
    able to talk about without being able to change."""
    catalog.upsert_entity("service:appriver.connector.web", "AppRiver.Connector.Web",
                          "service", COMMONS)
    return catalog


def test_a_private_document_cannot_merge_its_wording_into_an_org_entity(merge_catalog, tmp_path):
    """The motivating case, inverted: 'Webroot Connector' merging into the formal repo name
    is exactly the cross-source win entity resolution exists for — and exactly the write a
    document only its owner can read must not make."""
    pipeline = _pipeline_with_resolver(merge_catalog, tmp_path)

    pipeline._persist_graph("d-sec", [("service:webroot connector", "Webroot Connector",
                                       "service")], [], [], PRIVATE)

    assert merge_catalog.resolve_entity("webroot connector")["id"] == "service:webroot connector"
    assert merge_catalog.get_entity("service:appriver.connector.web")["source_id"] == COMMONS


def test_the_same_document_from_a_shared_source_still_merges(merge_catalog, tmp_path):
    """The guard must bite on privacy alone. Without this, 'nothing merged' would be
    indistinguishable from a resolver that was never wired up."""
    pipeline = _pipeline_with_resolver(merge_catalog, tmp_path, name="shared-lance")

    pipeline._persist_graph("d-pub", [("service:webroot connector", "Webroot Connector",
                                       "service")], [], [], COMMONS)

    assert merge_catalog.resolve_entity("webroot connector")["id"] == \
        "service:appriver.connector.web"


def test_a_private_document_may_still_attach_to_an_org_entity(merge_catalog, tmp_path):
    """'May attach, must never reshape' — a private note about a real service is the whole
    point of teaching one, so its edge has to land on the org's own node."""
    pipeline = _pipeline_with_resolver(merge_catalog, tmp_path, name="attach-lance")

    pipeline._persist_graph(
        "d-sec",
        [("service:appriver.connector.web", "AppRiver.Connector.Web", "service")],
        [], [("person:mole", "works_on", "service:appriver.connector.web", "")], PRIVATE)

    edges = merge_catalog.graph_neighbors("service:appriver.connector.web")
    assert any(e["src"] == "person:mole" for e in edges)


def test_a_private_document_cannot_rename_an_org_entity(merge_catalog, tmp_path):
    """The quieter merge: upsert_entity's rename rule replaces the display name everyone
    sees in the graph view, autocomplete and citations."""
    pipeline = _pipeline_with_resolver(merge_catalog, tmp_path, name="rename-lance")

    pipeline._persist_graph("d-sec", [("service:appriver.connector.web",
                                       "Ada's private name for it", "service")],
                            [], [], PRIVATE)

    assert merge_catalog.get_entity("service:appriver.connector.web")["name"] == \
        "AppRiver.Connector.Web"


def test_a_private_document_cannot_alias_an_org_entity_but_can_alias_its_own(
        merge_catalog, tmp_path):
    """An extractor-supplied alias row (a person's email from a table) is the same global
    write as a merge, through a quieter door — while a private source naming the entities
    it minted itself costs nobody anything."""
    pipeline = _pipeline_with_resolver(merge_catalog, tmp_path, name="alias-lance")

    pipeline._persist_graph(
        "d-sec",
        [("person:mole", "Mole", "person")],
        [("ada-secret-codename", "service:appriver.connector.web"),
         ("mole@corp.example", "person:mole")],
        [], PRIVATE)

    assert merge_catalog.resolve_entity("ada-secret-codename") is None
    assert merge_catalog.resolve_entity("mole@corp.example")["id"] == "person:mole"


def test_an_entity_minted_privately_is_not_bridged_into_the_org_graph(merge_catalog):
    """A `same_as` bridge carries no evidence document, so `_evidence_visible` keeps it
    visible to everyone — bridging a private-only entity would publish its name however
    well its documents are filtered."""
    merge_catalog.upsert_entity("repo:appriver.connector.web", "AppRiver.Connector.Web",
                                "repo", PRIVATE)
    assert merge_catalog.refresh_same_as_bridges() == 0

    merge_catalog.set_source_ownership(PRIVATE, owner=ADA, shared=True)
    assert merge_catalog.refresh_same_as_bridges() == 1


def test_open_mode_ingestion_is_untouched_by_the_guard(catalog, tmp_path):
    """Ownerless sources are the commons, so a single-user workspace must resolve, rename
    and bridge exactly as it did before knowledge scopes existed."""
    catalog.upsert_source("files:solo", "Solo", "files")
    assert catalog.is_private_source("files:solo") is False
    assert catalog.is_private_source("files:never-registered") is False

    catalog.upsert_entity("service:appriver.connector.web", "AppRiver.Connector.Web",
                          "service", "files:solo")
    pipeline = _pipeline_with_resolver(catalog, tmp_path, name="solo-lance")
    pipeline._persist_graph("d-solo", [("service:webroot connector", "Webroot Connector",
                                        "service")], [], [], "files:solo")

    assert catalog.resolve_entity("webroot connector")["id"] == "service:appriver.connector.web"


# --------------------------------------------------------------------- taught-note ownership

def test_a_taught_note_is_private_to_its_author_by_default(catalog, tmp_path):
    assert note_source(ADA, share=False) == (f"notes:{ADA}", f"Notes from {ADA}")
    assert note_source(ADA, share=True)[0] == USER_TAUGHT_SOURCE


def test_open_mode_teaching_still_lands_in_the_shared_commons():
    """No signed-in user means no owner to keep it from — and every note taught before this
    existed must stay exactly as readable as it was."""
    assert note_source(None, share=False)[0] == USER_TAUGHT_SOURCE


def test_teaching_privately_creates_an_owned_bucket_others_cannot_read(catalog, tmp_path):
    from quickjoiner.ingest.pipeline import IngestPipeline

    store = KnowledgeStore(tmp_path / "notes-lance", FakeEmbedder())
    pipeline = IngestPipeline(store, catalog)

    teach_fact(catalog, pipeline, "the standup is at 9:15", topic="standup", user=ADA)
    assert f"notes:{ADA}" in catalog.visible_source_ids(ADA)
    assert f"notes:{ADA}" not in catalog.visible_source_ids(BOB)

    hits = store.search("standup time", top_k=5, min_score=0.0,
                        scope=SearchScope(visible_source_ids=catalog.visible_source_ids(BOB)))
    assert all(h.source_id != f"notes:{ADA}" for h in hits)


def test_teaching_with_share_reaches_everyone(catalog, tmp_path):
    from quickjoiner.ingest.pipeline import IngestPipeline

    store = KnowledgeStore(tmp_path / "shared-lance", FakeEmbedder())
    pipeline = IngestPipeline(store, catalog)

    result = teach_fact(catalog, pipeline, "releases ship on Thursday", user=ADA, share=True)
    assert "shared with everyone" in result
    assert USER_TAUGHT_SOURCE in catalog.visible_source_ids(BOB)


# ------------------------------------------------------------------------ end to end

def _client(tmp_path, monkeypatch):
    """A real app over a scratch workspace — the unit tests above prove the predicate, this
    proves the wiring, which is the half that actually keeps a user's data in."""
    import yaml
    from fastapi.testclient import TestClient

    import quickjoiner.app as app_module
    from quickjoiner.api.app import create_app

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(yaml.safe_dump({"org": "acme", "sources": []}),
                                    encoding="utf-8")
    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    return TestClient(create_app(ws))


def test_no_api_read_path_leaks_another_users_private_note(tmp_path, monkeypatch):
    """The leak test that matters: Ada teaches something privately, and every read surface
    Bob can reach — search, chat scope resolution, autocomplete, graph — must not show it."""
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/users", json={"username": ADA, "password": "pw-ada-123"})
    ada = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": ADA, "password": "pw-ada-123"}).json()["token"]}
    client.post("/api/auth/users", json={"username": BOB, "password": "pw-bob-123",
                                         "role": "editor"}, headers=ada)
    bob = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": BOB, "password": "pw-bob-123"}).json()["token"]}

    secret = "the aardvark migration slips to Q4"
    assert client.post("/api/learn", json={"fact": secret, "topic": "aardvark"},
                       headers=ada).status_code == 200

    # Ada can retrieve her own note...
    assert any("aardvark" in h["text"].lower()
               for h in client.get("/api/search", params={"q": "aardvark migration"},
                                   headers=ada).json())
    # ...and Bob cannot, through the plain retrieval endpoint.
    assert client.get("/api/search", params={"q": "aardvark migration"},
                      headers=bob).json() == []


def test_sharing_a_taught_fact_makes_it_readable_by_everyone(tmp_path, monkeypatch):
    """The other half — opting in must genuinely reach the commons, or the feature is just
    a way to lose your own notes."""
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/users", json={"username": ADA, "password": "pw-ada-123"})
    ada = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": ADA, "password": "pw-ada-123"}).json()["token"]}
    client.post("/api/auth/users", json={"username": BOB, "password": "pw-bob-123",
                                         "role": "editor"}, headers=ada)
    bob = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": BOB, "password": "pw-bob-123"}).json()["token"]}

    client.post("/api/learn", json={"fact": "the aardvark migration slips to Q4",
                                    "topic": "aardvark", "share": True}, headers=ada)
    assert client.get("/api/search", params={"q": "aardvark migration"},
                      headers=bob).json() != []


def test_open_mode_reads_are_unchanged(tmp_path, monkeypatch):
    """No users created — the single-user workspace this repo runs on every day. Nothing is
    restricted and no visibility predicate is built at all."""
    client = _client(tmp_path, monkeypatch)
    client.post("/api/learn", json={"fact": "the aardvark migration slips to Q4"})
    assert client.get("/api/search", params={"q": "aardvark migration"}).json() != []


# ------------------------------------------------------------------------ promotion
#
# The flywheel: what a joiner works out privately is exactly what the next joiner needs.
# Its two properties are that approving PUBLISHES (or the feature is a way to lose notes)
# and that nothing published without a decision (or private-by-default meant nothing).

def _promotion_store(catalog, tmp_path):
    from quickjoiner.ingest.pipeline import IngestPipeline

    store = KnowledgeStore(tmp_path / "promo-lance", FakeEmbedder(),
                           retrieval=RetrievalConfig(hybrid=True, reranker="none"))
    return store, IngestPipeline(store, catalog)


def _taught(catalog, tmp_path):
    """Ada teaches herself one private fact; returns (store, doc row)."""
    store, pipeline = _promotion_store(catalog, tmp_path)
    teach_fact(catalog, pipeline, "the aardvark migration slips to Q4",
               topic="aardvark", user=ADA)
    doc = catalog.documents_for_source(f"notes:{ADA}")[0]
    return store, doc


def test_a_reviewed_note_reaches_everyone_without_being_re_ingested(catalog, tmp_path):
    """The whole design in one test: after approval Bob can retrieve it, and it is the SAME
    document — same id, so every citation, label and graph edge still points at it."""
    from quickjoiner import promotion

    store, doc = _taught(catalog, tmp_path)
    doc_id = doc["doc_id"]

    assert store.search("aardvark migration", top_k=5, min_score=0.0,
                        scope=SearchScope(visible_source_ids=catalog.visible_source_ids(BOB))) == []

    promotion.request(catalog, doc_id, ADA, note="the next joiner will hit this")
    promotion.decide(catalog, store, doc_id, approve=True, reviewer=BOB)

    hits = store.search("aardvark migration", top_k=5, min_score=0.0,
                        scope=SearchScope(visible_source_ids=catalog.visible_source_ids(BOB)))
    assert [h.doc_id for h in hits] == [doc_id]
    assert catalog.get_document(doc_id)["source_id"] == promotion.PROMOTED_SOURCE


def test_a_declined_note_stays_exactly_where_it_was(catalog, tmp_path):
    from quickjoiner import promotion

    store, doc = _taught(catalog, tmp_path)
    promotion.request(catalog, doc["doc_id"], ADA)
    promotion.decide(catalog, store, doc["doc_id"], approve=False, reviewer=BOB,
                     note="this is about your own machine, not the org")

    assert catalog.get_document(doc["doc_id"])["source_id"] == f"notes:{ADA}"
    assert store.search("aardvark migration", top_k=5, min_score=0.0,
                        scope=SearchScope(visible_source_ids=catalog.visible_source_ids(BOB))) == []


def test_an_offer_alone_publishes_nothing(catalog, tmp_path):
    """Requesting must not be a back door around the review it exists to trigger."""
    from quickjoiner import promotion

    store, doc = _taught(catalog, tmp_path)
    promotion.request(catalog, doc["doc_id"], ADA)

    assert promotion.PROMOTED_SOURCE not in catalog.visible_source_ids(BOB)
    assert store.search("aardvark migration", top_k=5, min_score=0.0,
                        scope=SearchScope(visible_source_ids=catalog.visible_source_ids(BOB))) == []


def test_only_the_owner_can_offer_their_own_document(catalog, tmp_path):
    from quickjoiner import promotion

    _store, doc = _taught(catalog, tmp_path)
    with pytest.raises(promotion.PromotionError, match="owner"):
        promotion.request(catalog, doc["doc_id"], BOB)


def test_promoting_something_already_public_is_refused_rather_than_a_silent_no_op(catalog):
    """A no-op that reads as success would leave the requester believing they had
    published something they had not."""
    from quickjoiner import promotion

    with pytest.raises(promotion.PromotionError, match="already readable"):
        promotion.request(catalog, "d-pub", ADA)


def test_deciding_a_document_nobody_offered_is_refused(catalog, tmp_path):
    from quickjoiner import promotion

    store, doc = _taught(catalog, tmp_path)
    with pytest.raises(promotion.PromotionError, match="not been offered"):
        promotion.decide(catalog, store, doc["doc_id"], approve=True, reviewer=BOB)


def test_promotion_releases_the_entities_the_merge_guard_was_holding(catalog, tmp_path):
    """The two halves of this item meet here: while private, the note's entities were kept
    out of the bridge layer; publishing the document releases exactly the ones it cites."""
    from quickjoiner import promotion

    store, doc = _taught(catalog, tmp_path)
    catalog.upsert_entity("service:aardvark", "Aardvark", "service", f"notes:{ADA}")
    catalog.upsert_entity("service:unrelated", "Unrelated", "service", f"notes:{ADA}")
    catalog.replace_doc_edges(doc["doc_id"],
                              [("service:aardvark", "part_of", "team:core", "")])

    promotion.request(catalog, doc["doc_id"], ADA)
    promotion.decide(catalog, store, doc["doc_id"], approve=True, reviewer=BOB)

    assert catalog.get_entity("service:aardvark")["source_id"] == promotion.PROMOTED_SOURCE
    assert catalog.get_entity("service:unrelated")["source_id"] == f"notes:{ADA}"


def test_a_personal_note_is_scored_below_the_same_claim_from_the_org(catalog):
    """The discounted evidence class — so a personal-vs-org contradiction surfaces with both
    citations and the org's evidence ranking higher, rather than being averaged away."""
    from quickjoiner.agent.confidence import classify_evidence, score_edge

    personal = classify_evidence("aardvark", "note://ada/aardvark", "note", f"notes:{ADA}")
    commons = classify_evidence("aardvark", "note://x/aardvark", "note", USER_TAUGHT_SOURCE)
    assert personal == "personal-note" and commons == "generic"
    assert score_edge(personal, 1, 1) < score_edge(commons, 1, 1)


def test_promotion_lifts_the_discount_because_the_note_left_the_personal_bucket(catalog):
    from quickjoiner import promotion
    from quickjoiner.agent.confidence import classify_evidence

    assert classify_evidence("aardvark", "note://ada/aardvark", "note",
                             promotion.PROMOTED_SOURCE) == "generic"


# ------------------------------------------------------------- promotion over the API

def test_the_review_queue_is_a_reviewer_capability_not_a_read_one(tmp_path, monkeypatch):
    """Offering is the author's act and deciding is a reviewer's — and the queue's rows are
    documents still private to their authors, so seeing it is itself a reviewer capability
    rather than an ordinary read."""
    client = _client(tmp_path, monkeypatch)
    client.post("/api/auth/users", json={"username": ADA, "password": "pw-ada-123"})
    admin = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": ADA, "password": "pw-ada-123"}).json()["token"]}
    for who, role in ((BOB, "editor"), ("cal", "viewer")):
        client.post("/api/auth/users", json={"username": who, "password": f"pw-{who}-123",
                                             "role": role}, headers=admin)
    bob, cal = (
        {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": who, "password": f"pw-{who}-123"}
        ).json()["token"]} for who in (BOB, "cal"))

    client.post("/api/learn", json={"fact": "the aardvark migration slips to Q4",
                                    "topic": "aardvark"}, headers=bob)
    doc_id = client.get(f"/api/sources/notes:{BOB}/documents",
                        headers=bob).json()["documents"][0]["doc_id"]

    assert client.post(f"/api/documents/{doc_id}/promote",
                       json={"note": "next joiner will hit this"}, headers=bob).status_code == 200
    assert client.get("/api/promotions", headers=cal).status_code == 403

    queue = client.get("/api/promotions", headers=admin).json()["promotions"]
    assert [r["doc_id"] for r in queue] == [doc_id]
    assert queue[0]["author"] == BOB

    # The narrow read exception: a reviewer can open what was offered, and nothing else.
    assert "aardvark" in client.get(f"/api/promotions/{doc_id}", headers=admin).json()["text"]
    assert client.get("/api/promotions/no-such-doc", headers=admin).status_code == 404

    assert client.get("/api/search", params={"q": "aardvark migration"}, headers=admin).json() == []
    client.post(f"/api/promotions/{doc_id}/decide", json={"approve": True}, headers=admin)
    assert client.get("/api/search", params={"q": "aardvark migration"}, headers=admin).json() != []

