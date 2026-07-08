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
    summary, facts = parse_compression(
        "SUMMARY:\nDiscussed deploy cadence.\n\nFACTS:\n- Deploys happen Fridays\n- Rollbacks via #deploy-help\n"
    )
    assert summary == "Discussed deploy cadence."
    assert facts == ["Deploys happen Fridays", "Rollbacks via #deploy-help"]

    summary, facts = parse_compression("just prose, no sections")
    assert summary == "just prose, no sections" and facts == []

    digest = fallback_digest(_turns(2))
    assert "user: question 0" in digest and "assistant: answer 1" in digest


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
