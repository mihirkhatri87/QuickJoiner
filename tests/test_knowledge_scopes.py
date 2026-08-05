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
