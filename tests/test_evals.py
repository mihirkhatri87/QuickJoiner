"""Eval harness: evalset loading, retrieval metrics, agent-layer behavior checks."""

import pytest

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.app import AppContext
from quickjoiner.config import Config
from quickjoiner.connectors.base import Document
from quickjoiner.evals.harness import (
    TEMPLATE,
    load_evalset,
    run_agent_eval,
    run_eval,
    run_retrieval_eval,
    summarize_agent,
    summarize_retrieval,
)
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import ChatResult

from tests.test_agent_loop import ScriptedProvider

EVALSET = """\
name: acme-v1
cases:
  - id: deploys
    question: We deploy with Octopus on Fridays.
    expect:
      uris: ["wiki.acme.test/deploys"]
      keywords: ["Octopus", "Friday"]
  - id: unknown
    question: zebra quantum posture
    expect:
      refusal: true
"""


@pytest.fixture
def ctx(workspace, catalog, store):
    config = Config()
    config.retrieval.min_score = 0.5  # FakeEmbedder: exact-token queries score high
    return AppContext(
        workspace=workspace,
        config=config,
        catalog=catalog,
        store=store,
        pipeline=IngestPipeline(store, catalog),
    )


@pytest.fixture
def learned_ctx(ctx):
    ctx.pipeline.ingest(
        [
            Document(
                uri="https://wiki.acme.test/deploys",
                title="Deploys",
                text="We deploy with Octopus on Fridays.",
            ),
            Document(
                uri="https://wiki.acme.test/oncall",
                title="On-call",
                text="The on-call rotation swaps every Monday morning.",
            ),
        ],
        "test:src",
    )
    return ctx


@pytest.fixture
def evalset_path(tmp_path):
    path = tmp_path / "evals.yaml"
    path.write_text(EVALSET, encoding="utf-8")
    return path


def test_load_evalset(evalset_path):
    name, cases = load_evalset(evalset_path)
    assert name == "acme-v1" and len(cases) == 2
    assert cases[0].uris == ["wiki.acme.test/deploys"] and cases[0].refusal is False
    assert cases[1].refusal is True


def test_template_is_a_valid_evalset(tmp_path):
    path = tmp_path / "starter.yaml"
    path.write_text(TEMPLATE, encoding="utf-8")
    name, cases = load_evalset(path)
    assert name == "my-org-evals" and len(cases) == 3
    multihop = next(c for c in cases if c.hops)
    assert multihop.hops == ["proj-a", "nautical-models", "octopus"]


def test_multi_hop_eval_set_file_loads():
    # The shipped cross-source eval set must always parse (it's the measurement loop).
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "evals" / "multi-hop-crosssource.yaml"
    name, cases = load_evalset(path)
    assert name == "multi-hop-crosssource"
    assert any(len(c.hops) >= 2 for c in cases)  # genuinely multi-hop
    assert any(c.refusal for c in cases)         # honesty cases present


def test_empty_evalset_rejected(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("name: x\ncases: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no cases"):
        load_evalset(path)


def test_retrieval_eval_hits_and_refusals(learned_ctx, evalset_path):
    _, cases = load_evalset(evalset_path)
    results = run_retrieval_eval(learned_ctx, cases)

    deploys = next(r for r in results if r.case_id == "deploys")
    assert deploys.hit_rank == 1 and deploys.cleared_threshold

    unknown = next(r for r in results if r.case_id == "unknown")
    assert unknown.refusal_correct is True  # nothing clears the threshold

    summary = summarize_retrieval(results)
    assert summary["recall_at_k"] == 1.0 and summary["mrr"] == 1.0
    assert summary["grounded_recall"] == 1.0 and summary["refusal_accuracy"] == 1.0


def test_retrieval_eval_reports_misses(learned_ctx):
    from quickjoiner.evals.harness import EvalCase

    results = run_retrieval_eval(
        learned_ctx,
        [EvalCase(id="miss", question="We deploy with Octopus on Fridays.",
                  uris=["jira.acme.test/PAY-99"])],
    )
    assert results[0].hit_rank is None and not results[0].cleared_threshold
    assert summarize_retrieval(results)["recall_at_k"] == 0.0


def test_retrieval_hop_coverage(learned_ctx):
    from quickjoiner.evals.harness import EvalCase

    # learned_ctx has a "Deploys" doc and an "On-call" doc; a cross-source question
    # touching both should show full hop coverage.
    results = run_retrieval_eval(
        learned_ctx,
        [EvalCase(id="multi", question="deploy octopus rotation monday",
                  hops=["deploys", "oncall"])],
    )
    assert results[0].hop_coverage == 1.0
    summary = summarize_retrieval(results)
    assert summary["hop_coverage"] == 1.0 and summary["multi_hop_cases"] == 1


def test_agent_hop_coverage(learned_ctx):
    from quickjoiner.evals.harness import EvalCase

    provider = ScriptedProvider([ChatResult(text="See the deploys wiki and the oncall page.")])
    agent = OnboardingAgent(provider, [], system="sys")
    results = run_agent_eval(
        learned_ctx, [EvalCase(id="m", question="q", hops=["deploys", "oncall"])], agent=agent
    )
    assert results[0].hop_coverage == 1.0
    assert summarize_agent(results, [EvalCase(id="m", question="q", hops=["deploys", "oncall"])])[
        "hop_coverage"
    ] == 1.0


def test_agent_eval_citations_keywords_and_refusals(learned_ctx, evalset_path):
    _, cases = load_evalset(evalset_path)
    provider = ScriptedProvider(
        [
            ChatResult(
                text="We deploy with Octopus every Friday "
                     "[https://wiki.acme.test/deploys]."
            ),
            ChatResult(text="I haven't learned that yet."),
        ]
    )
    agent = OnboardingAgent(provider, [], system="sys")
    results = run_agent_eval(learned_ctx, cases, agent=agent)

    deploys = next(r for r in results if r.case_id == "deploys")
    assert deploys.cited is True and deploys.keywords_missing == []
    assert deploys.refusal_correct is True  # answerable question was not refused

    unknown = next(r for r in results if r.case_id == "unknown")
    assert unknown.refused and unknown.refusal_correct

    summary = summarize_agent(results, cases)
    assert summary["citation_rate"] == 1.0 and summary["keyword_coverage"] == 1.0
    assert summary["false_refusal_rate"] == 0.0 and summary["refusal_accuracy"] == 1.0


def test_agent_eval_flags_false_refusal_and_missing_keywords(learned_ctx, evalset_path):
    _, cases = load_evalset(evalset_path)
    provider = ScriptedProvider(
        [
            ChatResult(text="I haven't learned that yet."),  # false refusal of answerable case
            ChatResult(text="Deploys happen weekly."),        # should have refused
        ]
    )
    agent = OnboardingAgent(provider, [], system="sys")
    results = run_agent_eval(learned_ctx, cases, agent=agent)
    summary = summarize_agent(results, cases)
    assert summary["false_refusal_rate"] == 1.0
    assert summary["refusal_accuracy"] == 0.0


def test_is_refusal_matches_typographic_apostrophe():
    # Live models (observed against a real gpt-oss broker) render the mandated
    # refusal phrase with a mix of straight and curly apostrophes even within
    # one eval run — the harness must not undercount real refusals over a
    # punctuation glyph.
    from quickjoiner.evals.harness import is_refusal

    assert is_refusal("I haven’t learned that yet.")
    assert is_refusal("I haven't learned that information yet.")
    assert not is_refusal("Deploys happen weekly.")


# -- threshold calibration -------------------------------------------------------

def _answerable(cid, hit_score):
    from quickjoiner.evals.harness import RetrievalCaseResult

    return RetrievalCaseResult(
        cid, hit_rank=1, top_score=hit_score, cleared_threshold=True,
        refusal_correct=None, hit_score=hit_score,
    )


def _refusal(cid, top_score):
    from quickjoiner.evals.harness import RetrievalCaseResult

    # refusal_correct is recomputed per-threshold by calibrate; the stored value is unused.
    return RetrievalCaseResult(
        cid, hit_rank=None, top_score=top_score, cleared_threshold=False,
        refusal_correct=False, hit_score=None,
    )


def test_calibrate_clean_separation():
    from quickjoiner.evals.harness import calibrate

    results = [_answerable("a", 0.70), _answerable("b", 0.75),
               _refusal("r", 0.40), _refusal("s", 0.45)]
    cal = calibrate(results)
    # Everything in (0.45, 0.70] grounds both hits and refuses both refusals, so the
    # optimal band is [0.46, 0.70] and the recommendation is its midpoint (0.58) — NOT
    # the brittle top edge (0.70). Both metrics still perfect at the midpoint.
    assert cal["plateau"] == [0.46, 0.70]
    assert cal["threshold"] == 0.58
    assert cal["grounded_recall"] == 1.0 and cal["refusal_accuracy"] == 1.0
    assert cal["floor_met"] is True


def test_calibrate_curve_is_monotone():
    from quickjoiner.evals.harness import calibrate

    cal = calibrate([_answerable("a", 0.6), _answerable("b", 0.72),
                     _refusal("r", 0.5), _refusal("s", 0.66)])
    grs = [p["gr"] for p in cal["curve"]]
    ras = [p["ra"] for p in cal["curve"]]
    assert all(grs[i] >= grs[i + 1] for i in range(len(grs) - 1))  # grounded recall falls
    assert all(ras[i] <= ras[i + 1] for i in range(len(ras) - 1))  # refusal accuracy rises


def test_calibrate_picks_plateau_midpoint_not_edge():
    from quickjoiner.evals.harness import calibrate

    # A wide plateau (objective == 1.0 across [0.31, 0.80]) — the recommendation must be
    # the midpoint (~0.55), NOT the brittle top edge (0.80). This is the toy-set case
    # that a naive tie-break-high would blow up to 0.80.
    cal = calibrate([_answerable("a", 0.80), _refusal("r", 0.30)])
    assert cal["plateau"] == [0.31, 0.80]
    assert cal["threshold"] == 0.55  # midpoint, ties → lower t
    assert cal["thin"] is True       # 1 answerable + 1 refusal is far below the floor


def test_calibrate_floor_unreachable_returns_safest():
    from quickjoiner.evals.harness import calibrate

    # A refusal that outscores the whole sweep — the floor can never be met.
    cal = calibrate([_answerable("a", 0.6), _refusal("r", 0.95)])
    assert cal["floor_met"] is False
    assert cal["threshold"] == 0.80  # safest (max refusal accuracy), tie-broken high


def test_run_eval_includes_calibration_block(learned_ctx, evalset_path):
    report = run_eval(learned_ctx, evalset_path, calibrate_threshold=True)
    assert "calibration" in report
    cal = report["calibration"]
    assert 0.30 <= cal["threshold"] <= 0.80
    assert cal["floor_met"] is True  # learned_ctx separates the deploy hit from the refusal


def test_calibrate_apply_persists_min_score(learned_ctx, evalset_path):
    report = run_eval(learned_ctx, evalset_path, calibrate_threshold=True)
    new_t = report["calibration"]["threshold"]
    learned_ctx.config.retrieval.min_score = new_t
    learned_ctx.catalog.save_config(learned_ctx.config)
    assert learned_ctx.catalog.load_config().retrieval.min_score == new_t


# -- report comparison -----------------------------------------------------------

def test_compare_flags_regression():
    from quickjoiner.evals.harness import compare_reports

    old = {"retrieval": {"summary": {"grounded_recall": 0.90, "refusal_accuracy": 1.0}}}
    new = {"retrieval": {"summary": {"grounded_recall": 0.80, "refusal_accuracy": 1.0}}}
    diff = compare_reports(old, new)
    assert diff["regressed"] is True
    row = next(r for r in diff["rows"] if r["metric"] == "grounded_recall")
    assert row["delta"] == -0.1 and row["regressed"] is True


def test_compare_no_regression_within_tolerance():
    from quickjoiner.evals.harness import compare_reports

    old = {"retrieval": {"summary": {"grounded_recall": 0.90}}}
    new = {"retrieval": {"summary": {"grounded_recall": 0.89}}}  # -0.01, within 2pt
    assert compare_reports(old, new)["regressed"] is False


def test_compare_improvement_not_flagged():
    from quickjoiner.evals.harness import compare_reports

    old = {"retrieval": {"summary": {"grounded_recall": 0.80}}}
    new = {"retrieval": {"summary": {"grounded_recall": 0.95}}}
    diff = compare_reports(old, new)
    assert diff["regressed"] is False
    row = next(r for r in diff["rows"] if r["metric"] == "grounded_recall")
    assert row["delta"] == 0.15


def test_compare_false_refusal_rate_is_lower_is_better():
    from quickjoiner.evals.harness import compare_reports

    worse = compare_reports(
        {"agent": {"summary": {"false_refusal_rate": 0.10}}},
        {"agent": {"summary": {"false_refusal_rate": 0.30}}},  # rose -> regression
    )
    assert worse["regressed"] is True
    better = compare_reports(
        {"agent": {"summary": {"false_refusal_rate": 0.30}}},
        {"agent": {"summary": {"false_refusal_rate": 0.10}}},  # fell -> improvement
    )
    assert better["regressed"] is False


def test_run_eval_saves_report(learned_ctx, evalset_path):
    provider = ScriptedProvider(
        [
            ChatResult(text="Octopus on Friday [https://wiki.acme.test/deploys]"),
            ChatResult(text="I haven't learned that yet."),
        ]
    )
    agent = OnboardingAgent(provider, [], system="sys")
    report = run_eval(learned_ctx, evalset_path, agent_layer=True, agent=agent)

    assert report["retrieval"]["summary"]["recall_at_k"] == 1.0
    assert report["agent"]["summary"]["refusal_accuracy"] == 1.0
    assert report["config"]["min_score"] == 0.5

    import json
    from pathlib import Path

    saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert saved["name"] == "acme-v1" and "agent" in saved
