"""Eval harness: measure how well the current workspace answers from learned knowledge.

Two layers, run independently:

- **retrieval** (deterministic, no LLM): for each question, does memory surface the
  expected source in the top-k, at what rank, and does it clear the grounding
  threshold? For should-refuse questions: does nothing clear the threshold?
  Metrics: recall@k, grounded recall (cleared min_score), MRR, refusal accuracy.

- **agent** (needs an LLM): runs the real tool-calling agent end-to-end and checks
  behavior — did it refuse when it should, did it cite the expected sources, did
  the answer mention the expected keywords, did it falsely refuse an answerable
  question?

Eval sets are YAML (see TEMPLATE / `qj eval --init`). Reports are saved as JSON
under <workspace>/evals/ so runs can be compared over time (e.g. before/after
changing the embedding model, threshold, or chunking).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# The system prompt mandates this phrasing for ungrounded questions.
REFUSAL_MARKERS = ("haven't learned", "have not learned", "not learned that yet")

TEMPLATE = """\
# QuickJoiner eval set. Run with: qj eval <this file> [--agent]
#
# Each case is a question plus what a correct outcome looks like:
#   uris:     substrings of the expected source URI or title (any one matching counts)
#   keywords: strings the agent's answer should contain (agent layer only)
#   hops:     substrings for the DISTINCT sources a correct multi-hop answer must touch
#             (all counted -> hop_coverage metric; use for cross-source questions)
#   refusal:  true when the CORRECT behavior is "I haven't learned that yet."
name: my-org-evals
cases:
  - id: deploy-process
    question: How do we deploy to production, and on what schedule?
    expect:
      uris: ["wiki", "confluence"]
      keywords: ["Octopus", "Friday"]
  - id: cross-source-multihop
    question: Does proj-a depend on the nautical-models package, and where is it deployed?
    expect:
      keywords: ["Nautical.Models", "Production"]
      hops: ["proj-a", "nautical-models", "octopus"]   # consumer repo -> package -> deploy
  - id: unlearned-topic
    question: What is the Kubernetes cost budget for Q3?
    expect:
      refusal: true
"""


@dataclass
class EvalCase:
    id: str
    question: str
    uris: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    refusal: bool = False
    # Multi-hop / cross-source: substrings for the distinct sources a correct answer
    # must touch (e.g. the consumer repo AND the deploy dashboard). Unlike `uris`
    # ("any one counts"), hop_coverage measures how MANY of these were surfaced/cited.
    hops: list[str] = field(default_factory=list)


@dataclass
class RetrievalCaseResult:
    case_id: str
    hit_rank: int | None  # 1-based rank of the first expected source in top-k; None = miss
    top_score: float
    cleared_threshold: bool  # the expected hit scored >= retrieval.min_score
    refusal_correct: bool | None  # only set for refusal cases
    hop_coverage: float | None = None  # fraction of expected hops surfaced in top-k


@dataclass
class AgentCaseResult:
    case_id: str
    refused: bool
    refusal_correct: bool  # refusal cases: refused; answerable cases: did NOT refuse
    cited: bool | None  # expected uri substring present in the answer (None: no uris given)
    keywords_missing: list[str]
    answer: str
    hop_coverage: float | None = None  # fraction of expected hops cited in the answer


def load_evalset(path: Path | str) -> tuple[str, list[EvalCase]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases = []
    for raw in data.get("cases", []):
        expect = raw.get("expect", {}) or {}
        cases.append(
            EvalCase(
                id=str(raw.get("id") or raw["question"][:48]),
                question=str(raw["question"]),
                uris=[str(u) for u in expect.get("uris", [])],
                keywords=[str(k) for k in expect.get("keywords", [])],
                refusal=bool(expect.get("refusal", False)),
                hops=[str(h) for h in expect.get("hops", [])],
            )
        )
    if not cases:
        raise ValueError(f"Eval set {path} contains no cases")
    return str(data.get("name", Path(path).stem)), cases


# -- retrieval layer -------------------------------------------------------------

def run_retrieval_eval(ctx, cases: list[EvalCase]) -> list[RetrievalCaseResult]:
    retrieval = ctx.config.retrieval
    results = []
    for case in cases:
        hits = ctx.store.search(case.question, top_k=retrieval.top_k, min_score=0.0)
        top_score = hits[0].score if hits else 0.0
        if case.refusal:
            grounded = any(h.score >= retrieval.min_score for h in hits)
            results.append(
                RetrievalCaseResult(case.id, None, top_score, False, refusal_correct=not grounded)
            )
            continue
        hit_rank, cleared = None, False
        for rank, hit in enumerate(hits, start=1):
            if any(u in hit.uri or u in hit.title for u in case.uris):
                hit_rank, cleared = rank, hit.score >= retrieval.min_score
                break
        hop_cov = None
        if case.hops:
            surfaced = " ".join(f"{h.uri} {h.title}" for h in hits)
            hop_cov = round(sum(1 for hop in case.hops if hop in surfaced) / len(case.hops), 3)
        results.append(
            RetrievalCaseResult(case.id, hit_rank, top_score, cleared, None, hop_coverage=hop_cov)
        )
    return results


def summarize_retrieval(results: list[RetrievalCaseResult]) -> dict:
    answerable = [r for r in results if r.refusal_correct is None]
    refusals = [r for r in results if r.refusal_correct is not None]
    summary: dict = {"cases": len(results), "answerable": len(answerable), "refusal_cases": len(refusals)}
    if answerable:
        summary["recall_at_k"] = round(sum(1 for r in answerable if r.hit_rank) / len(answerable), 3)
        summary["grounded_recall"] = round(
            sum(1 for r in answerable if r.cleared_threshold) / len(answerable), 3
        )
        summary["mrr"] = round(
            sum(1 / r.hit_rank for r in answerable if r.hit_rank) / len(answerable), 3
        )
    multihop = [r for r in results if r.hop_coverage is not None]
    if multihop:
        summary["multi_hop_cases"] = len(multihop)
        summary["hop_coverage"] = round(sum(r.hop_coverage for r in multihop) / len(multihop), 3)
    if refusals:
        summary["refusal_accuracy"] = round(
            sum(1 for r in refusals if r.refusal_correct) / len(refusals), 3
        )
    return summary


# -- agent layer -----------------------------------------------------------------

def is_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def run_agent_eval(
    ctx,
    cases: list[EvalCase],
    agent=None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> list[AgentCaseResult]:
    if agent is None:
        agent = ctx.build_agent(provider_override, model_override)
    results = []
    for case in cases:
        answer, _ = agent.ask(case.question)  # fresh history per case: no cross-contamination
        refused = is_refusal(answer)
        cited = any(u in answer for u in case.uris) if (case.uris and not case.refusal) else None
        missing = [] if case.refusal else [
            k for k in case.keywords if k.lower() not in answer.lower()
        ]
        hop_cov = None
        if case.hops and not case.refusal:
            low = answer.lower()
            hop_cov = round(sum(1 for hop in case.hops if hop.lower() in low) / len(case.hops), 3)
        results.append(
            AgentCaseResult(
                case_id=case.id,
                refused=refused,
                refusal_correct=refused if case.refusal else not refused,
                cited=cited,
                keywords_missing=missing,
                answer=answer,
                hop_coverage=hop_cov,
            )
        )
    return results


def summarize_agent(results: list[AgentCaseResult], cases: list[EvalCase]) -> dict:
    by_id = {c.id: c for c in cases}
    answerable = [r for r in results if not by_id[r.case_id].refusal]
    refusals = [r for r in results if by_id[r.case_id].refusal]
    summary: dict = {"cases": len(results)}
    if answerable:
        summary["false_refusal_rate"] = round(
            sum(1 for r in answerable if r.refused) / len(answerable), 3
        )
        with_uris = [r for r in answerable if r.cited is not None]
        if with_uris:
            summary["citation_rate"] = round(
                sum(1 for r in with_uris if r.cited) / len(with_uris), 3
            )
        keyword_total = sum(len(by_id[r.case_id].keywords) for r in answerable)
        if keyword_total:
            found = keyword_total - sum(len(r.keywords_missing) for r in answerable)
            summary["keyword_coverage"] = round(found / keyword_total, 3)
        multihop = [r for r in answerable if r.hop_coverage is not None]
        if multihop:
            summary["multi_hop_cases"] = len(multihop)
            summary["hop_coverage"] = round(
                sum(r.hop_coverage for r in multihop) / len(multihop), 3
            )
    if refusals:
        summary["refusal_accuracy"] = round(
            sum(1 for r in refusals if r.refusal_correct) / len(refusals), 3
        )
    return summary


# -- full run ---------------------------------------------------------------------

def run_eval(
    ctx,
    evalset_path: Path | str,
    agent_layer: bool = False,
    agent=None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Run the eval set; save and return the JSON report."""
    name, cases = load_evalset(evalset_path)
    retrieval_results = run_retrieval_eval(ctx, cases)
    report = {
        "name": name,
        "generated": datetime.now(timezone.utc).isoformat(),
        "config": {
            "embedding": f"{ctx.config.embedding.provider}/{ctx.config.embedding.resolved_model()}",
            "min_score": ctx.config.retrieval.min_score,
            "top_k": ctx.config.retrieval.top_k,
        },
        "retrieval": {
            "summary": summarize_retrieval(retrieval_results),
            "cases": [asdict(r) for r in retrieval_results],
        },
    }
    if agent_layer:
        agent_results = run_agent_eval(ctx, cases, agent, provider_override, model_override)
        report["agent"] = {
            "summary": summarize_agent(agent_results, cases),
            "cases": [asdict(r) for r in agent_results],
        }

    import json

    evals_dir = ctx.workspace / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = evals_dir / f"{name}-{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report
