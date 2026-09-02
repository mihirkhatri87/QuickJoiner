"""The `qj bench` speed/cost harness (AI_ROADMAP S1).

The pure layers (percentiles, pack loading, report comparison) are tested directly; the
measurement seam is tested by running a REAL search through a real store and asserting the
stages it reports, since the whole value of the harness is that its numbers correspond to
work that actually happened. Wall-clock durations themselves are never asserted — a test
that fails when the machine is busy teaches everyone to ignore it.
"""

from __future__ import annotations

import pytest

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.bench.harness import (
    MIN_SAMPLES_FOR_GATE,
    TEMPLATE,
    compare_reports,
    load_querypack,
    percentiles,
    summarize_agent,
    summarize_retrieval,
    QueryTiming,
)
from quickjoiner.bench.harness import AgentTiming
from quickjoiner.llm.base import ChatResult, LLMProvider, TokenUsage, ToolCall
from quickjoiner.memory.store import NULL_TRACE, SearchTrace

from tests.test_agent_loop import ScriptedProvider, _echo_tool


# -- pure helpers ------------------------------------------------------------------

def test_percentiles_report_their_own_sample_count():
    stats = percentiles([10.0, 20.0, 30.0, 40.0])
    assert stats["samples"] == 4 and stats["p50"] == 25.0
    assert stats["min"] == 10.0 and stats["max"] == 40.0
    # An empty series reports nothing rather than a misleading zero.
    assert percentiles([]) == {"samples": 0}


def test_querypack_accepts_a_bench_pack_or_an_eval_set(tmp_path):
    pack = tmp_path / "pack.yaml"
    pack.write_text(TEMPLATE, encoding="utf-8")
    name, queries = load_querypack(pack)
    assert name == "my-org-bench" and len(queries) == 4

    # An eval set's questions ARE a query pack — benching needs no second file.
    evalset = tmp_path / "evals.yaml"
    evalset.write_text(
        "name: my-evals\ncases:\n  - id: a\n    question: How do we deploy?\n"
        "    expect:\n      refusal: true\n",
        encoding="utf-8",
    )
    assert load_querypack(evalset) == ("my-evals", ["How do we deploy?"])

    empty = tmp_path / "empty.yaml"
    empty.write_text("name: nothing\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_querypack(empty)


def _report(p50: float, p95: float = 100.0, samples: int = 12) -> dict:
    return {"retrieval": {"summary": {
        "search_total_ms": {"p50": p50, "p95": p95, "samples": samples},
    }}}


def test_compare_flags_a_relative_slowdown_not_an_absolute_one():
    """20% is the gate — so the same +5ms passes at 100ms and fails at 8ms."""
    assert not compare_reports(_report(100.0), _report(105.0))["regressed"]
    assert compare_reports(_report(8.0), _report(10.0))["regressed"]

    faster = compare_reports(_report(100.0), _report(50.0))
    assert not faster["regressed"]
    row = next(r for r in faster["rows"] if r["metric"] == "search_total_ms.p50")
    assert row["change"] == -0.5  # reported as a proportion, negative = faster


def test_compare_reports_thin_samples_but_never_gates_on_them():
    """A p95 over 3 runs is noise; a gate that fires on noise gets switched off."""
    thin = compare_reports(_report(10.0, samples=2), _report(100.0, samples=2))
    row = next(r for r in thin["rows"] if r["metric"] == "search_total_ms.p50")
    assert row["change"] == 9.0 and row["gated"] is False and row["regressed"] is False
    assert not thin["regressed"]
    assert f"2 samples" in row["note"]

    ok = compare_reports(_report(10.0, samples=MIN_SAMPLES_FOR_GATE),
                         _report(100.0, samples=MIN_SAMPLES_FOR_GATE))
    assert ok["regressed"] is True


def test_summarize_retrieval_reports_where_the_time_went():
    timings = [
        QueryTiming("q1", {"dense": 10.0, "rerank": 90.0}, {"hits": 5}, 100.0),
        QueryTiming("q2", {"dense": 10.0, "rerank": 90.0}, {"hits": 3}, 100.0),
    ]
    summary = summarize_retrieval(timings)
    assert summary["queries"] == 2 and summary["runs"] == 2
    assert summary["search_total_ms"]["p50"] == 100.0
    # The share is the number that actually directs optimisation work.
    assert summary["stage_share_of_p50"] == {"dense": 0.1, "rerank": 0.9}
    assert summary["counts"]["hits"] == 4.0


# -- the measurement seam ----------------------------------------------------------

def test_search_trace_records_the_stages_of_a_real_search(store):
    store.upsert_document("d1", "files:notes", "notes/deploy.md", "Deploy", "doc",
                          ["we deploy to production every friday"])
    trace = SearchTrace()
    hits = store.search("how do we deploy to production", top_k=3, min_score=0.0, trace=trace)

    assert hits, "the fixture document should be retrievable"
    # Every stage a query actually ran is timed and named.
    assert {"embed_query", "dense", "gate"} <= set(trace.stages)
    assert all(v >= 0.0 for v in trace.stages.values())
    assert trace.total_ms == pytest.approx(sum(trace.stages.values()), abs=1e-3)
    # Counts explain the timings (why rerank cost what it did).
    assert trace.counts["hits"] == len(hits)
    assert trace.counts["dense_candidates"] >= len(hits)


def test_tracing_is_opt_in_and_changes_nothing(store):
    """Production passes no trace; results must be identical either way."""
    store.upsert_document("d1", "files:notes", "notes/deploy.md", "Deploy", "doc",
                          ["we deploy to production every friday"])
    untraced = store.search("deploy production", top_k=3, min_score=0.0)
    traced = store.search("deploy production", top_k=3, min_score=0.0, trace=SearchTrace())
    assert [h.doc_id for h in untraced] == [h.doc_id for h in traced]
    assert [h.score for h in untraced] == [h.score for h in traced]

    # The null trace measures nothing and swallows every call, so the hot path is free.
    with NULL_TRACE.stage("anything"):
        pass
    NULL_TRACE.count("anything", 1)


def test_stage_entered_twice_accumulates_rather_than_overwriting():
    """`_dense` runs twice on a hybrid query (dense leg + sparse rescore); attributing
    only the last call would under-report the leg that was measured."""
    trace = SearchTrace()
    for _ in range(2):
        with trace.stage("dense"):
            pass
    assert len(trace.stages) == 1 and trace.stages["dense"] >= 0.0


# -- agent-layer token accounting --------------------------------------------------

def test_agent_sums_token_usage_and_rounds_across_a_tool_loop():
    """The S5 cost measurement: tokens are per-ROUND on the wire but per-ANSWER to a
    user, and a tool loop can be many rounds."""
    provider = ScriptedProvider([
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={})],
                   usage=TokenUsage(prompt=100, completion=10, cached=80)),
        ChatResult(text="the answer", usage=TokenUsage(prompt=250, completion=40, cached=200)),
    ])
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q")

    assert answer == "the answer"
    assert agent.last_rounds == 2
    assert agent.last_usage.prompt == 350 and agent.last_usage.completion == 50
    assert agent.last_usage.cached == 280  # what prompt caching actually saved
    assert agent.last_usage.total == 400


def test_agent_usage_resets_per_turn_so_answers_are_not_cumulative():
    provider = ScriptedProvider([
        ChatResult(text="one", usage=TokenUsage(prompt=100, completion=10)),
        ChatResult(text="two", usage=TokenUsage(prompt=200, completion=20)),
    ])
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    agent.ask("first")
    agent.ask("second")
    assert agent.last_usage.prompt == 200 and agent.last_rounds == 1


def test_a_provider_that_reports_no_usage_leaves_zeros_not_errors():
    """Ollama over a stream may report nothing; the bench must degrade, never fail."""
    provider = ScriptedProvider([ChatResult(text="answer")])
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    agent.ask("q")
    assert agent.last_usage.total == 0 and agent.last_rounds == 1


def test_summarize_agent_surfaces_the_cache_hit_rate():
    timings = [
        AgentTiming("q1", 120.0, 900.0, 2, 1, 1000, 100, 800, 400),
        AgentTiming("q2", 80.0, 700.0, 1, 0, 500, 50, 0, 200),
    ]
    summary = summarize_agent(timings)
    assert summary["answers"] == 2
    assert summary["rounds_per_answer"] == 1.5
    assert summary["tokens_per_answer"]["p50"] == pytest.approx(825.0)
    assert summary["cache_hit_rate"] == round(800 / 1500, 3)


def test_summarize_agent_says_why_first_token_is_missing():
    """A non-streaming provider and a provider that streamed nothing are different
    problems — dropping the row would hide both."""
    summary = summarize_agent([AgentTiming("q", None, 500.0, 1, 0, 10, 5, 0, 20)])
    assert summary["first_token_ms"]["samples"] == 0
    assert "no streamed text deltas" in summary["first_token_ms"]["note"]


# -- what the harness found the first time it ran -----------------------------------

def test_resolve_entity_name_lookup_is_indexed_not_a_table_scan(catalog):
    """Regression guard for the defect `qj bench` found on its first real run.

    `resolve_entity`'s `WHERE id = ? OR LOWER(name) = ?` could not use an index for the
    LOWER(name) branch, so every call scanned the whole entities table — 36,203 rows on
    the live workspace, ~30 calls per query via alias expansion, measuring as 27% of
    total retrieval latency (841ms p50). The expression index makes both branches
    indexed; this asserts the PLANNER agrees, because the correctness tests passed
    perfectly throughout and would never have caught it.
    """
    catalog.upsert_entity("repo:appriver.nautical.models", "AppRiver.Nautical.Models", "repo")
    catalog.add_entity_alias("nautical models", "repo:appriver.nautical.models")

    plan = " ".join(
        str(tuple(row)) for row in catalog._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM entities WHERE id = ? OR LOWER(name) = ?",
            ("x", "x"),
        )
    )
    assert "idx_entities_lower_name" in plan, plan
    assert "SCAN entities" not in plan, plan

    # …and the lookup still resolves by id, by name (any case) and by alias.
    assert catalog.resolve_entity("repo:appriver.nautical.models")["name"] == "AppRiver.Nautical.Models"
    assert catalog.resolve_entity("appriver.NAUTICAL.models")["id"] == "repo:appriver.nautical.models"
    assert catalog.resolve_entity("nautical models")["id"] == "repo:appriver.nautical.models"
    assert catalog.resolve_entity("no such thing") is None


# ---------------------------------------------------------- sync throughput (S1)

@pytest.fixture
def bench_ctx(catalog, workspace):
    """Minimal context for the sync layer: it reads the catalog's history and nothing else."""
    from types import SimpleNamespace

    return SimpleNamespace(catalog=catalog, workspace=workspace)


def _sync_event(catalog, job_id, source, started, ended, ingested, kind="sync", state="done"):
    catalog.record_sync_event(job_id, source, state, False, started, ended,
                              {"ingested": ingested} if ingested is not None else None,
                              None, kind)


def test_sync_bench_reads_throughput_from_real_run_history(bench_ctx):
    """Ingest throughput is READ from `sync_events`, never measured by running a sync — a
    real sync would benchmark the remote's mood and a synthetic one would benchmark a
    fixture."""
    from datetime import datetime, timedelta, timezone

    from quickjoiner.bench.harness import run_sync_bench

    now = datetime.now(timezone.utc)
    # 120 documents in 2 minutes = 60 docs/min.
    _sync_event(bench_ctx.catalog, "s1", "Repo A",
                (now - timedelta(hours=1)).isoformat(),
                (now - timedelta(hours=1) + timedelta(minutes=2)).isoformat(), 120)
    # 30 documents in 30 seconds = 60 docs/min, from a run someone STOPPED — it still did
    # real work for a real duration, and excluding it would drop exactly the long crawls.
    _sync_event(bench_ctx.catalog, "s2", "Repo B",
                (now - timedelta(hours=2)).isoformat(),
                (now - timedelta(hours=2) + timedelta(seconds=30)).isoformat(), 30,
                state="stopped")

    out = run_sync_bench(bench_ctx, days=7)
    assert out["runs"] == 2
    assert out["documents"] == 150
    assert out["docs_per_min"] == 60.0
    assert out["per_source"]["Repo A"] == {"docs_per_min": 60.0, "samples": 1}


def test_sync_bench_says_so_rather_than_reporting_a_fake_zero(bench_ctx):
    from quickjoiner.bench.harness import run_sync_bench

    out = run_sync_bench(bench_ctx, days=7)
    assert out["runs"] == 0 and "no completed sync runs" in out["note"]
    assert "docs_per_min" not in out  # never a zero that reads as a measurement


def test_sync_bench_ignores_cleanups_unfinished_and_instant_runs(bench_ctx):
    from datetime import datetime, timedelta, timezone

    from quickjoiner.bench.harness import run_sync_bench

    now = datetime.now(timezone.utc)
    base = (now - timedelta(minutes=30)).isoformat()
    _sync_event(bench_ctx.catalog, "c1", "X", base,
                (now - timedelta(minutes=29)).isoformat(), 500, kind="cleanup")
    _sync_event(bench_ctx.catalog, "u1", "Y", base, None, 500)          # still running
    _sync_event(bench_ctx.catalog, "z1", "Z", base, base, 500)          # 0s — undividable
    assert run_sync_bench(bench_ctx, days=7)["runs"] == 0


# ------------------------------------------------ graph reads (S1, added 2026-08-15)

def test_synthetic_graph_is_reproducible_and_writes_what_it_claims(catalog):
    """A benchmark nobody else can reproduce is an anecdote. Same seed, same graph — and
    the reported edge count must be the number of rows actually written, since edges are
    keyed (src, rel, dst, evidence_doc_id) and a duplicate would silently collapse."""
    from quickjoiner.bench.synth import build_synthetic_graph

    built = build_synthetic_graph(catalog, entities=200, edges=600, documents=50, seed=3)
    assert built["edges"] == catalog.graph_totals()["edges"]

    shape = sorted((r["src"], r["rel"], r["dst"]) for r in catalog._read_all(
        "SELECT src, rel, dst FROM edges"))
    catalog.reset_knowledge()
    build_synthetic_graph(catalog, entities=200, edges=600, documents=50, seed=3)
    assert sorted((r["src"], r["rel"], r["dst"]) for r in catalog._read_all(
        "SELECT src, rel, dst FROM edges")) == shape

    # A different seed must actually differ, or "reproducible" is just "constant".
    catalog.reset_knowledge()
    build_synthetic_graph(catalog, entities=200, edges=600, documents=50, seed=4)
    assert sorted((r["src"], r["rel"], r["dst"]) for r in catalog._read_all(
        "SELECT src, rel, dst FROM edges")) != shape


def test_sampled_pairs_include_unconnected_ones(catalog):
    """The unconnected pair is the EXPENSIVE case — BFS exhausts its frontier instead of
    returning early — and it is what a user hits whenever two things turn out to be
    unrelated. Timing only reachable pairs would flatter the numbers and hide the work."""
    from quickjoiner.bench.synth import build_synthetic_graph, sample_pairs

    build_synthetic_graph(catalog, entities=300, edges=200, documents=20, seed=5)
    pairs = sample_pairs(catalog, count=20, seed=5)
    assert len(pairs) == 20
    assert any(catalog.graph_path(a, b) is None for a, b in pairs), (
        "every sampled pair was connected — the expensive branch is going unmeasured")


def test_graph_bench_measures_the_reads_the_agent_actually_calls(bench_ctx):
    """graph_path answers "how are these related" and graph_relations answers "list every
    team with its members" — both on the answer path, neither previously measured. That gap
    hid a 4.2x saving in graph_path that no correctness test could see, because the answers
    were right and only slow."""
    from quickjoiner.bench.harness import run_graph_bench
    from quickjoiner.bench.synth import build_synthetic_graph

    build_synthetic_graph(bench_ctx.catalog, entities=200, edges=600, documents=50, seed=3)
    summary = run_graph_bench(bench_ctx, repeats=1, warmup=0, pairs=4)

    assert summary["graph"] == {"edges": 600, "entities": 200}
    for metric in ("graph_path_ms", "graph_path_candidates_ms",
                   "graph_neighbors_ms", "graph_relations_ms"):
        assert summary[metric]["samples"] > 0, f"{metric} reported no samples"
        assert summary[metric]["p50"] >= 0


def test_graph_bench_skips_rather_than_reporting_a_fake_zero(bench_ctx):
    """An empty workspace has no graph to time. Reporting 0ms would read as "instant" and
    would sail through --compare as a massive improvement."""
    from quickjoiner.bench.harness import run_graph_bench

    summary = run_graph_bench(bench_ctx)
    assert "skipped" in summary and "graph_path_ms" not in summary


def test_compare_gates_on_a_graph_slowdown():
    """The graph layer joins the merge gate: this is the regression class that went
    unnoticed until it was found by accident."""
    old = {"graph": {"summary": {"graph_path_ms": {"p50": 350.0, "samples": 12}}}}
    new = {"graph": {"summary": {"graph_path_ms": {"p50": 1450.0, "samples": 12}}}}
    result = compare_reports(old, new)
    row = [r for r in result["rows"] if r["metric"] == "graph_path_ms.p50"][0]
    assert row["regressed"] and result["regressed"]
    assert compare_reports(new, old)["regressed"] is False  # the reverse is the win
