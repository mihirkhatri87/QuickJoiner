"""Sessions, projects, conversation memory, and context compression."""

import pytest

from quickjoiner.app import AppContext
from quickjoiner.config import Config
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import ChatResult, ToolCall
from quickjoiner.sessions import (
    SessionManager,
    estimate_tokens,
    fallback_digest,
    messages_from_json,
    messages_to_json,
    parse_compression,
    split_for_compression,
)

from tests.test_agent_loop import ScriptedProvider


@pytest.fixture
def ctx(workspace, catalog, store):
    config = Config()
    config.retrieval.min_score = 0.0
    return AppContext(
        workspace=workspace,
        config=config,
        catalog=catalog,
        store=store,
        pipeline=IngestPipeline(store, catalog),
    )


@pytest.fixture
def manager(ctx):
    return SessionManager(ctx)


def _turns(n):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"question {i} about deploys and rollbacks"})
        msgs.append({"role": "assistant", "content": f"answer {i} citing [wiki/deploys]"})
    return msgs


# -- serialization ---------------------------------------------------------------

def test_message_roundtrip_with_tool_calls():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c1", name="echo", input={"v": 1})]},
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "out"},
        {"role": "assistant", "content": "done"},
    ]
    restored = messages_from_json(messages_to_json(messages))
    assert restored[1]["tool_calls"][0] == ToolCall(id="c1", name="echo", input={"v": 1})
    assert restored[2]["content"] == "out" and restored[3]["content"] == "done"


def test_tool_results_truncated_on_load():
    messages = [{"role": "tool", "tool_call_id": "c", "name": "t", "content": "x" * 5000}]
    restored = messages_from_json(messages_to_json(messages), tool_result_max_chars=100)
    assert len(restored[0]["content"]) < 200 and restored[0]["content"].endswith("…[truncated]")


def test_split_for_compression_respects_tool_groups():
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c", name="t", input={})]},
        {"role": "tool", "tool_call_id": "c", "name": "t", "content": "r"},
        {"role": "assistant", "content": "a2"},
    ]
    old, recent = split_for_compression(messages, keep_recent=3)
    # Cut moves forward to the next user turn: the tool group is never separated.
    assert [m["role"] for m in old] == ["user", "assistant"]
    assert recent[0]["content"] == "q2" and len(recent) == 4


def test_parse_compression_and_fallback():
    summary, facts, triples = parse_compression(
        "SUMMARY:\nDiscussed deploy cadence.\n\nFACTS:\n- Deploys happen Fridays\n- Rollbacks via #deploy-help\n"
    )
    assert summary == "Discussed deploy cadence."
    assert facts == ["Deploys happen Fridays", "Rollbacks via #deploy-help"]
    assert triples == []

    summary, facts, triples = parse_compression("just prose, no sections")
    assert summary == "just prose, no sections" and facts == [] and triples == []


def test_parse_compression_relationships_section():
    from quickjoiner.sessions import Triple, parse_triples

    summary, facts, triples = parse_compression(
        "SUMMARY:\nOwnership chat.\n\nFACTS:\n- The payments guild owns proj-a\n\n"
        "RELATIONSHIPS:\n"
        "- team: payments guild | owns | repo: proj-a\n"
        "- person: Meena | works_on | service: checkout\n"
        "- alien: zork | eats | repo: proj-a\n"
        "- team: sre | conquers | repo: proj-a\n"
        "- free text that is not a triple\n"
    )
    assert summary == "Ownership chat."
    assert facts == ["The payments guild owns proj-a"]  # FACTS bullets don't leak into triples
    assert triples == [
        Triple("team", "payments guild", "owns", "repo", "proj-a"),
        Triple("person", "Meena", "works_on", "service", "checkout"),
    ]  # off-vocabulary type/relation lines and prose are dropped, never invented
    assert parse_triples(["The payments guild owns proj-a"]) == []

    digest = fallback_digest(_turns(2))
    assert "user: question 0" in digest and "assistant: answer 1" in digest


def test_parse_triples_pubsub_and_storage_vocab():
    """The 2026-07-17 vocab additions: pub/sub verbs to `topic`, stores_in to
    `datastore` — and near-miss synonyms stay dropped (widened, not opened)."""
    from quickjoiner.sessions import Triple, parse_triples

    triples = parse_triples([
        "service: checkout | publishes_to | topic: order-events",
        "service: billing | subscribes_to | topic: order-events",
        "service: checkout | stores_in | datastore: OrdersDb",
        "service: checkout | writes_to | datastore: OrdersDb",   # off-vocab rel -> dropped
        "service: billing | publishes_to | queue: order-events", # off-vocab type -> dropped
        "service: billing | stores_in | database: OrdersDb",     # off-vocab type -> dropped
    ])
    assert triples == [
        Triple("service", "checkout", "publishes_to", "topic", "order-events"),
        Triple("service", "billing", "subscribes_to", "topic", "order-events"),
        Triple("service", "checkout", "stores_in", "datastore", "OrdersDb"),
    ]


def test_parse_triples_enforces_relation_signatures():
    """AI #21: the type check and the relation check used to be independent, so a line
    whose three words were each in-vocabulary passed even when the combination is a
    category error. Signatures close that hole without narrowing the vocabulary."""
    from quickjoiner.ingest.triples import Triple, parse_triples

    triples = parse_triples([
        # the motivating impossible line from the roadmap: every word legal, shape absurd
        "environment: prod | owns | person: bob",
        "person: bob | owns | service: checkout",      # same relation, right way round
        # the single most common real error on the live corpus (548 edges): inverted
        # `works_on` — a ticket is worked ON, it does not work on anything
        "ticket: NAUT-1 | works_on | person: meena",
        "person: meena | works_on | ticket: NAUT-1",
        "ticket: NAUT-1 | deploys | environment: prod",  # a ticket doesn't deploy
        "service: checkout | deploys | environment: prod",
        "topic: order-events | publishes_to | service: billing",  # publisher/topic inverted
        "service: billing | publishes_to | topic: order-events",
        "service: checkout | publishes_to | service: payments",  # you publish to a topic
    ])
    assert triples == [
        Triple("person", "bob", "owns", "service", "checkout"),
        Triple("person", "meena", "works_on", "ticket", "NAUT-1"),
        Triple("service", "checkout", "deploys", "environment", "prod"),
        Triple("service", "billing", "publishes_to", "topic", "order-events"),
    ]


def test_signatures_admit_the_shapes_real_org_prose_actually_uses():
    """Calibration guard (2026-07-30). The first cut of this table was drawn around an
    idealised ontology and, measured against the live 109k-edge graph, threw away 1,809
    perfectly sensible statements. These are the highest-volume ones it was wrong about —
    they must keep validating, or the table has drifted back toward tidy-but-lossy."""
    from quickjoiner.ingest.triples import signature_allows

    for src, rel, dst in [
        ("person", "owns", "ticket"),          # ×47 live — ticket ownership is standard
        ("team", "owns", "ticket"),            # ×38
        ("person", "works_on", "team"),        # ×246
        ("service", "part_of", "environment"), # ×328 — "part of the LDAP-build environment"
        ("environment", "provides", "service"),# ×74 — a host provides a service
        ("team", "provides", "service"),       # ×49
        ("repo", "provides", "project"),       # ×50
        ("project", "deploys", "service"),     # ×45 — deployment-tooling language
        ("service", "depends_on", "environment"),  # ×39
    ]:
        assert signature_allows(src, rel, dst), f"{src} | {rel} | {dst} should be admitted"


def test_every_relation_is_signed_or_deliberately_unsigned():
    """A new verb added to TRIPLE_RELS must declare its signature (or be listed as
    deliberately unrestricted) — otherwise it would silently bypass the new gate."""
    from quickjoiner.ingest.triples import (
        RELATION_SIGNATURES,
        TRIPLE_RELS,
        TRIPLE_TYPES,
        UNSIGNED_RELS,
    )

    assert set(RELATION_SIGNATURES) | UNSIGNED_RELS == TRIPLE_RELS
    assert not (set(RELATION_SIGNATURES) & UNSIGNED_RELS)
    for rel, (domain, range_) in RELATION_SIGNATURES.items():
        assert domain <= TRIPLE_TYPES and range_ <= TRIPLE_TYPES, rel


def test_deterministic_extractor_edges_satisfy_their_signatures():
    """The connectors emit these same verbs without going through parse_triples, so the
    table must not declare a shape the shipped extractors already contradict (Octopus
    service->deploys->environment, the ADO/Jira ticket hierarchy, deps.py maps)."""
    from quickjoiner.ingest.triples import signature_allows

    for src, rel, dst in [
        ("service", "deploys", "environment"),   # octopus dashboard
        ("ticket", "part_of", "project"),        # jira issue -> project
        ("ticket", "part_of", "ticket"),         # ADO/Jira Epic hierarchy
        ("repo", "depends_on", "package"),       # deps.py dependency map
        ("repo", "provides", "package"),
        ("repo", "publishes_to", "topic"),       # ingest/pubsub.py
        ("repo", "subscribes_to", "topic"),
        ("repo", "stores_in", "datastore"),
    ]:
        assert signature_allows(src, rel, dst), f"{src} {rel} {dst}"


def test_compress_prompt_vocab_in_lockstep_with_validator():
    """Both LLM prompts enumerate the vocabulary from the sets themselves — a new
    verb/type must appear in the prompts without any hand-edit."""
    from quickjoiner.ingest.triples import (
        DOC_TRIPLE_SYSTEM,
        SIGNATURE_LINES,
        TRIPLE_RELS,
        TRIPLE_TYPES,
    )
    from quickjoiner.sessions import COMPRESS_SYSTEM

    for vocab_word in TRIPLE_RELS | TRIPLE_TYPES:
        assert vocab_word in COMPRESS_SYSTEM
        assert vocab_word in DOC_TRIPLE_SYSTEM
    # ...and so must the signatures, for the same reason.
    assert SIGNATURE_LINES in COMPRESS_SYSTEM and SIGNATURE_LINES in DOC_TRIPLE_SYSTEM


# -- projects & session lifecycle -------------------------------------------------

def test_project_create_and_session_grouping(manager, ctx):
    project = manager.create_project("Payments Onboarding", "Ramp-up on the payments platform")
    assert project["id"] == "payments-onboarding"

    sess = manager.open_session(project="Payments Onboarding")
    assert sess["project_id"] == "payments-onboarding"
    assert ctx.catalog.list_sessions("payments-onboarding")[0]["id"] == sess["id"]

    system = manager.system_context(sess)
    assert "Payments Onboarding" in system and "Ramp-up" in system


def test_session_persists_and_resumes(manager):
    sess = manager.open_session()
    manager.record_turn(sess["id"], _turns(2))
    reloaded = manager.open_session(sess["id"])
    assert reloaded["title"].startswith("question 0")
    history = manager.history(reloaded)
    assert len(history) == 4 and history[-1]["content"] == "answer 1 citing [wiki/deploys]"
    assert reloaded["est_tokens"] == estimate_tokens(history)


# -- compression & conversation memory --------------------------------------------

def test_compression_folds_history_and_learns_facts(manager, ctx):
    ctx.config.chat.compress_after_est_tokens = 50  # force compression
    ctx.config.chat.keep_recent_messages = 4
    sess = manager.open_session(project=None)
    manager.record_turn(sess["id"], _turns(6))

    provider = ScriptedProvider(
        [ChatResult(text="SUMMARY:\nEarly questions covered deploy cadence [wiki/deploys].\n\nFACTS:\n- Deploys go out on Fridays\n")]
    )
    assert manager.maybe_compress(sess["id"], provider=provider) is True

    after = ctx.catalog.get_session(sess["id"])
    history = manager.history(after)
    assert len(history) == 4  # only recent turns kept verbatim
    assert "deploy cadence" in after["summary"]
    assert "Summary of this conversation" in manager.system_context(after)
    assert after["est_tokens"] < estimate_tokens(_turns(6))

    # Facts became searchable conversation memory with provenance.
    hits = ctx.store.search("Deploys go out on Fridays", top_k=5, min_score=0.0)
    assert any(h.uri.startswith("conversation://") for h in hits)
    sources = {s["id"] for s in ctx.catalog.list_sources()}
    assert "conversations:learned" in sources


def test_compression_skips_short_sessions(manager, ctx):
    sess = manager.open_session()
    manager.record_turn(sess["id"], _turns(1))
    assert manager.maybe_compress(sess["id"], provider=ScriptedProvider([])) is False


def test_compression_survives_provider_failure(manager, ctx):
    ctx.config.chat.compress_after_est_tokens = 10
    ctx.config.chat.keep_recent_messages = 2

    class ExplodingProvider(ScriptedProvider):
        def chat(self, *a, **kw):
            raise RuntimeError("no api key")

    sess = manager.open_session()
    manager.record_turn(sess["id"], _turns(4))
    assert manager.maybe_compress(sess["id"], provider=ExplodingProvider([])) is True
    after = ctx.catalog.get_session(sess["id"])
    assert "- user: question 0" in after["summary"]  # deterministic digest fallback


def test_distill_ingests_full_session(manager, ctx):
    sess = manager.open_session(project=None)
    manager.record_turn(sess["id"], _turns(2))
    provider = ScriptedProvider(
        [ChatResult(text="SUMMARY:\nTalked rollbacks.\n\nFACTS:\n- Rollbacks are coordinated in #deploy-help\n")]
    )
    assert manager.distill(sess["id"], provider) == 1
    hits = ctx.store.search("Rollbacks are coordinated", top_k=5, min_score=0.0)
    assert any("#deploy-help" in h.text for h in hits)

    with pytest.raises(ValueError, match="No session"):
        manager.distill("nope", provider)


def test_distill_persists_relationship_triples(manager, ctx):
    """Phase C: relationships the LLM extracts land in the knowledge graph with
    the conversation document as evidence, alias-resolvable like everything else."""
    sess = manager.open_session(project=None)
    manager.record_turn(sess["id"], _turns(2))
    provider = ScriptedProvider([ChatResult(text=(
        "SUMMARY:\nOwnership and deps discussed.\n\n"
        "FACTS:\n- The payments guild owns proj-a\n\n"
        "RELATIONSHIPS:\n"
        "- team: payments guild | owns | repo: proj-a\n"
        "- repo: proj-a | depends_on | package: AppRiver.Nautical.Models\n"
    ))])
    manager.distill(sess["id"], provider)

    guild = ctx.catalog.resolve_entity("payments guild")
    assert guild and guild["type"] == "team"
    rows = ctx.catalog.graph_neighbors("repo:proj-a")
    rels = {(r["src"], r["rel"]) for r in rows}
    assert ("team:payments guild", "owns") in rels
    assert ("repo:proj-a", "depends_on") in rels
    for r in rows:
        assert r["evidence_uri"].startswith("conversation://")  # cited to the conversation
    # dotted package name gets the org spoken-form alias
    assert ctx.catalog.resolve_entity("nautical models")["id"] == "package:appriver.nautical.models"
    # the relationships are also searchable text in the conversation doc
    hits = ctx.store.search("payments guild owns proj-a", top_k=5, min_score=0.0)
    assert any(h.uri.startswith("conversation://") for h in hits)
