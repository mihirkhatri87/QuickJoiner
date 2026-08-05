"""Bench harness: how FAST and how EXPENSIVELY this workspace answers.

The speed/cost twin of `quickjoiner/evals/harness.py`, and deliberately its mirror image
in shape — a YAML pack of inputs, a JSON report saved beside the eval reports, and a
`--compare` that exits non-zero on regression. The house rule it exists to serve
(AI_ROADMAP S1): no speed work merges without a before/after table, exactly as no quality
work merges without `qj eval --compare`. "Felt faster" is not a measurement.

Three layers, run independently:

- **retrieval** (no LLM, always runs): per-stage latency for each query — embed-query,
  dense leg, sparse leg, RRF fuse, sparse rescore, rerank, gate — plus alias expansion and
  graph expansion, which sit around `store.search` in the real `search_memory` path rather
  than inside it. Reported as p50/p95 per stage, so a regression names its own cause.

- **embed** (no LLM): raw embedder throughput in chunks/sec at a realistic batch. This is
  the ingest bottleneck and the number S6 (GPU/parallel ingestion) has to beat.

- **agent** (needs an LLM): time to first token, time to full answer, model rounds per
  answer, and tokens per answer including prompt-cache reads — the S5 cost-delta
  measurement, which needs real token counts and had no way to get them before.

WHAT THE NUMBERS MEAN. Latency is measured with `time.perf_counter` around real calls
against the real workspace, so it includes whatever else the machine is doing; run a pack
twice before believing a small delta. Percentiles over a handful of samples are indicative,
not statistical — `samples` is reported next to every one so a p95 over 6 points is
visibly that. Warm-up iterations are run and DISCARDED because the first search of a
process pays one-off costs that no user pays twice (the ~80MB cross-encoder loads lazily on
first `rank()`, LanceDB opens the table, the FTS sidecar backfills); folding those into a
p50 would make every "optimization" look like a win the moment it moved them around.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

TEMPLATE = """\
# QuickJoiner bench pack. Run with: qj bench <this file> [--agent]
#
# Just the questions — a bench measures cost, not correctness, so nothing is expected
# of the answers. Use REAL questions from your org: latency depends on how many
# candidates a query pulls and whether it triggers tool calls, so a pack of toy
# questions benchmarks a system nobody is using.
name: my-org-bench
queries:
  - How do we deploy to production?
  - Which services depend on the nautical-models package?
  - Who owns the provisioning repo?
  - What is the on-call rotation?
"""

# Text embedded by the embed-throughput leg. Realistic length matters (attention is not
# linear in tokens), so this is a paragraph, not a word.
_EMBED_SAMPLE = (
    "The provisioning service publishes to the account-events topic and stores its "
    "state in the primary SQL cluster. It is deployed by the release pipeline on the "
    "same schedule as the management console, and its owner is the platform team."
)

# Latency regressions are judged in RELATIVE terms — a 5ms rise means nothing at 500ms
# and everything at 8ms — unlike the eval harness's absolute 0.02 tolerance on rates.
COMPARE_TOLERANCE = 0.20  # >20% slower / more tokens on a watched metric fails the gate
# Metrics compared by `--compare`, all "lower is better". Stage timings are compared too
# (they are what a regression is diagnosed from) but only these fail the gate: a stage
# can legitimately move a lot when work shifts between stages, while the totals cannot.
COMPARE_METRICS = (
    ("retrieval", "search_total_ms.p50"),
    ("retrieval", "search_total_ms.p95"),
    ("embed", "ms_per_chunk"),
    ("agent", "first_token_ms.p50"),
    ("agent", "answer_ms.p50"),
    ("agent", "tokens_per_answer.mean"),
)
# Below this many samples a percentile is reported but not trusted as a gate — the
# same honesty the eval harness applies with CALIBRATION_MIN_CASES.
MIN_SAMPLES_FOR_GATE = 5


@dataclass
class QueryTiming:
    """One query's measured run: stage timings in ms, plus what it retrieved."""

    query: str
    stages: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    total_ms: float = 0.0


def load_querypack(path: Path | str) -> tuple[str, list[str]]:
    """Read a bench pack. Also accepts an EVAL set — its `cases[].question` list is
    exactly a query pack, so a workspace with eval cases can bench without a second file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    queries = [str(q) for q in (data.get("queries") or [])]
    if not queries:
        queries = [str(c["question"]) for c in (data.get("cases") or []) if c.get("question")]
    if not queries:
        raise ValueError(f"Bench pack {path} contains no queries (or eval cases)")
    return str(data.get("name", Path(path).stem)), queries


def percentiles(values: list[float]) -> dict:
    """p50/p95/mean/min/max over raw samples, with the sample count alongside.

    p95 of fewer than 20 points is really "the worst sample" — `samples` is reported so a
    reader can see that rather than inferring a precision the data doesn't have.
    """
    if not values:
        return {"samples": 0}
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


# -- retrieval layer ---------------------------------------------------------------

def run_retrieval_bench(ctx, queries: list[str], repeats: int = 3,
                        warmup: int = 1) -> list[QueryTiming]:
    """Time every retrieval stage for each query, `repeats` times after `warmup` discarded
    runs. Measures the same work `agent.tools.search_memory` does — alias expansion and
    graph expansion included, since both sit on the real query path even though neither
    lives inside `store.search`."""
    from quickjoiner.memory.store import SearchTrace

    retrieval = ctx.config.retrieval
    timings: list[QueryTiming] = []
    for _ in range(max(0, warmup)):
        for query in queries:
            _timed_query(ctx, query, retrieval, SearchTrace())
    for _ in range(max(1, repeats)):
        for query in queries:
            timings.append(_timed_query(ctx, query, retrieval, SearchTrace()))
    return timings


def _timed_query(ctx, query: str, retrieval, trace) -> QueryTiming:
    search_q = query
    if retrieval.alias_expansion:
        from quickjoiner.memory.expansion import expand_query

        with trace.stage("alias_expansion"):
            try:
                search_q = expand_query(ctx.catalog, query)
            except Exception:
                search_q = query
    hits = ctx.store.search(search_q, top_k=retrieval.top_k,
                            min_score=retrieval.min_score, trace=trace)
    if retrieval.graph_expansion and hits:
        # Only runs when there ARE grounded hits — mirroring search_memory, where graph
        # expansion can never turn a refusal into an answer. Benching it unconditionally
        # would report a cost the product never pays on a refusal.
        with trace.stage("graph_expansion"):
            try:
                ctx.catalog.graph_expand(list({h.doc_id for h in hits}),
                                         retrieval.graph_expansion_limit)
            except Exception:
                pass
    return QueryTiming(query=query, stages=dict(trace.stages),
                       counts=dict(trace.counts), total_ms=trace.total_ms)


def summarize_retrieval(timings: list[QueryTiming]) -> dict:
    """Per-stage and total percentiles, plus the stage share of p50 — which is the number
    that actually directs optimization work ("rerank is 71% of a query" beats "17ms")."""
    if not timings:
        return {"queries": 0}
    stage_names = sorted({s for t in timings for s in t.stages})
    stages = {name: percentiles([t.stages.get(name, 0.0) for t in timings])
              for name in stage_names}
    total = percentiles([t.total_ms for t in timings])
    summary: dict = {
        "queries": len({t.query for t in timings}),
        "runs": len(timings),
        "search_total_ms": total,
        "stages_ms": stages,
    }
    if total.get("p50"):
        summary["stage_share_of_p50"] = {
            name: round(stages[name]["p50"] / total["p50"], 3) for name in stage_names
        }
    for count in ("dense_candidates", "sparse_candidates", "fused", "reranked", "hits"):
        values = [t.counts[count] for t in timings if count in t.counts]
        if values:
            summary.setdefault("counts", {})[count] = round(statistics.fmean(values), 1)
    return summary


# -- embed throughput --------------------------------------------------------------

def run_embed_bench(ctx, batch: int = 32, repeats: int = 3, warmup: int = 1) -> dict:
    """Embedder throughput at a realistic batch size — the ingest bottleneck.

    Embeds the SAME sample text repeatedly on purpose: this measures the model's
    throughput, not the corpus's, so identical inputs keep the only variable the thing
    being benchmarked. (No cache sits between here and the ONNX session, so repetition
    doesn't make it look artificially fast.)
    """
    embedder = ctx.store.embedder
    texts = [_EMBED_SAMPLE] * max(1, batch)
    for _ in range(max(0, warmup)):
        embedder.embed(texts)
    durations = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        embedder.embed(texts)
        durations.append((time.perf_counter() - t0) * 1000)
    per_batch = percentiles(durations)
    return {
        "model": f"{ctx.config.embedding.provider}/{ctx.config.embedding.resolved_model()}",
        "device": getattr(ctx.config.embedding, "device", "auto"),
        "batch": len(texts),
        "batch_ms": per_batch,
        "ms_per_chunk": round(per_batch["p50"] / len(texts), 4),
        "chunks_per_sec": round(len(texts) / (per_batch["p50"] / 1000), 1) if per_batch["p50"] else None,
    }


# -- agent layer -------------------------------------------------------------------

@dataclass
class AgentTiming:
    query: str
    first_token_ms: float | None  # None when the provider didn't stream any text
    answer_ms: float
    rounds: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    answer_chars: int


def run_agent_bench(ctx, queries: list[str], agent=None, provider_override: str | None = None,
                    model_override: str | None = None) -> list[AgentTiming]:
    """End-to-end answer latency and token cost, one fresh turn per query.

    No warm-up and no repeats by default: a real LLM round trip costs real money and real
    seconds, and unlike the retrieval layer there is no lazily-loaded local model whose
    first call is unrepresentative. That makes these single samples — reported as such.
    """
    timings: list[AgentTiming] = []
    for query in queries:
        turn_agent = agent or ctx.build_agent(provider_override, model_override)
        first_token: float | None = None
        tool_calls = 0
        t0 = time.perf_counter()

        def on_event(kind: str, detail: str) -> None:
            nonlocal first_token, tool_calls
            if kind == "delta" and first_token is None:
                first_token = (time.perf_counter() - t0) * 1000
            elif kind == "tool_call":
                tool_calls += 1

        answer, _ = turn_agent.ask(query, on_event=on_event)
        elapsed = (time.perf_counter() - t0) * 1000
        usage = turn_agent.last_usage
        timings.append(AgentTiming(
            query=query,
            first_token_ms=round(first_token, 3) if first_token is not None else None,
            answer_ms=round(elapsed, 3),
            rounds=turn_agent.last_rounds,
            tool_calls=tool_calls,
            prompt_tokens=usage.prompt,
            completion_tokens=usage.completion,
            cached_tokens=usage.cached,
            answer_chars=len(answer),
        ))
    return timings


def summarize_agent(timings: list[AgentTiming]) -> dict:
    if not timings:
        return {"answers": 0}
    first = [t.first_token_ms for t in timings if t.first_token_ms is not None]
    summary: dict = {
        "answers": len(timings),
        "answer_ms": percentiles([t.answer_ms for t in timings]),
        "rounds_per_answer": round(statistics.fmean([t.rounds for t in timings]), 2),
        "tool_calls_per_answer": round(statistics.fmean([t.tool_calls for t in timings]), 2),
        "tokens_per_answer": percentiles(
            [float(t.prompt_tokens + t.completion_tokens) for t in timings]
        ),
        "prompt_tokens_total": sum(t.prompt_tokens for t in timings),
        "completion_tokens_total": sum(t.completion_tokens for t in timings),
        "cached_tokens_total": sum(t.cached_tokens for t in timings),
    }
    if first:
        summary["first_token_ms"] = percentiles(first)
    else:
        # Says WHY it is absent rather than dropping the row: a non-streaming provider
        # and a provider that streamed nothing are different problems.
        summary["first_token_ms"] = {"samples": 0, "note": "no streamed text deltas observed"}
    prompt_total = summary["prompt_tokens_total"]
    if prompt_total:
        # The only observable proof prompt caching is working (S5). 0.0 with caching
        # configured on means the prefix is not stable, or the backend isn't caching.
        summary["cache_hit_rate"] = round(summary["cached_tokens_total"] / prompt_total, 3)
    return summary


# -- comparison --------------------------------------------------------------------

def _dig(summary: dict, dotted: str):
    node = summary
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def compare_reports(old: dict, new: dict) -> dict:
    """Delta table between two bench reports — the S1 merge gate.

    Unlike the eval harness's absolute tolerance, regressions here are RELATIVE: every
    watched metric is lower-is-better, and a rise of more than COMPARE_TOLERANCE (20%)
    fails. A metric measured from fewer than MIN_SAMPLES_FOR_GATE samples is reported
    with its delta but never fails the gate — a p95 over 3 runs is noise, and a gate that
    fires on noise gets switched off, which costs more than it saves.
    """
    rows: list[dict] = []
    regressed = False
    for layer, metric in COMPARE_METRICS:
        old_sum = (old.get(layer) or {}).get("summary", {})
        new_sum = (new.get(layer) or {}).get("summary", {})
        o, n = _dig(old_sum, metric), _dig(new_sum, metric)
        if o is None and n is None:
            continue
        row: dict = {"layer": layer, "metric": metric, "old": o, "new": n,
                     "change": None, "regressed": False, "gated": True}
        if isinstance(o, (int, float)) and isinstance(n, (int, float)) and o > 0:
            row["change"] = round((n - o) / o, 3)  # +0.25 = 25% slower
            samples = _dig(new_sum, metric.rsplit(".", 1)[0] + ".samples")
            if isinstance(samples, int) and samples < MIN_SAMPLES_FOR_GATE:
                row["gated"] = False
                row["note"] = f"{samples} samples — reported, not gated"
            row["regressed"] = row["gated"] and row["change"] > COMPARE_TOLERANCE
            regressed = regressed or row["regressed"]
        rows.append(row)
    return {"rows": rows, "regressed": regressed, "tolerance": COMPARE_TOLERANCE}


# -- full run ----------------------------------------------------------------------

def run_sync_bench(ctx, days: int = 7) -> dict:
    """Ingest throughput (documents/minute) read from the sync history already in
    `sync_events` — never by running a sync.

    Benching ingestion by *performing* one would need a connector, its credentials and its
    remote's mood on the day, which measures the network more than the pipeline; and a
    synthetic sync measures a fixture. The real runs are already recorded with their start,
    end and document count, so this reads them. Partial runs (stopped/errored) count: they
    ingested real documents for a real duration, and excluding them would systematically
    drop exactly the long crawls whose throughput matters most.

    Honest about thin data rather than confident about noise: a source with no finished run
    in the window reports no rate at all instead of a zero, and `samples` rides beside every
    figure so a 1-run "median" can't be read as a trend.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    try:
        events = ctx.catalog.list_sync_events(since, limit=500)
    except Exception:  # history is an observability nicety; never fail a bench on it
        return {"runs": 0, "note": "sync history unavailable"}

    per_source: dict[str, list[float]] = {}
    runs = 0
    docs_total = 0
    seconds_total = 0.0
    for e in events:
        if e["kind"] != "sync" or not e["ended_at"]:
            continue
        stats = json.loads(e["stats_json"]) if e["stats_json"] else {}
        docs = stats.get("ingested")
        if not docs:  # nothing ingested, or a run predating ingested-in-stats
            continue
        try:
            elapsed = ((datetime.fromisoformat(e["ended_at"])
                        - datetime.fromisoformat(e["started_at"])).total_seconds())
        except (ValueError, TypeError):
            continue
        if elapsed < 1.0:  # too short to divide by meaningfully
            continue
        runs += 1
        docs_total += docs
        seconds_total += elapsed
        per_source.setdefault(e["source"], []).append(docs / (elapsed / 60.0))

    if not runs:
        return {"runs": 0, "window_days": days,
                "note": ("no completed sync runs with a document count in the window — "
                         "run a sync, or widen --sync-days")}
    return {
        "runs": runs,
        "window_days": days,
        "documents": docs_total,
        "docs_per_min": round(docs_total / (seconds_total / 60.0), 2),
        "per_source": {
            src: {"docs_per_min": round(statistics.median(rates), 2), "samples": len(rates)}
            for src, rates in sorted(per_source.items())
        },
    }


def run_bench(ctx, pack_path: Path | str, agent_layer: bool = False, repeats: int = 3,
              warmup: int = 1, agent=None, provider_override: str | None = None,
              model_override: str | None = None, embed: bool = True,
              sync_days: int = 7) -> dict:
    """Run the bench pack; save and return the JSON report."""
    from dataclasses import asdict

    name, queries = load_querypack(pack_path)
    timings = run_retrieval_bench(ctx, queries, repeats=repeats, warmup=warmup)
    stats = ctx.catalog.stats()
    report: dict = {
        "name": name,
        "generated": datetime.now(timezone.utc).isoformat(),
        # Latency is meaningless without the shape of the corpus and the knobs in force —
        # a p50 from a 200-chunk workspace says nothing about a 56k-chunk one.
        "config": {
            "embedding": f"{ctx.config.embedding.provider}/{ctx.config.embedding.resolved_model()}",
            "llm": f"{ctx.config.llm.provider}/{ctx.config.llm.resolved_model()}",
            "documents": stats.get("documents"),
            "chunks": stats.get("chunks"),
            "hybrid": ctx.config.retrieval.hybrid,
            "reranker": ctx.config.retrieval.reranker,
            "rerank_candidates": ctx.config.retrieval.rerank_candidates,
            "graph_expansion": ctx.config.retrieval.graph_expansion,
            "alias_expansion": ctx.config.retrieval.alias_expansion,
            "top_k": ctx.config.retrieval.top_k,
            "min_score": ctx.config.retrieval.min_score,
            "ann_min_rows": ctx.config.retrieval.ann_min_rows,
            "ann_refine_factor": ctx.config.retrieval.ann_refine_factor,
            "prompt_cache": ctx.config.llm.prompt_cache,
        },
        "retrieval": {
            "summary": summarize_retrieval(timings),
            "queries": [asdict(t) for t in timings],
        },
    }
    if embed:
        report["embed"] = {"summary": run_embed_bench(ctx, repeats=repeats, warmup=warmup)}
    # Ingest throughput, read from the sync history rather than measured by running one —
    # always included because it costs one indexed query and closes S1's other half.
    report["sync"] = {"summary": run_sync_bench(ctx, days=sync_days)}
    if agent_layer:
        agent_timings = run_agent_bench(ctx, queries, agent, provider_override, model_override)
        report["agent"] = {
            "summary": summarize_agent(agent_timings),
            "answers": [asdict(t) for t in agent_timings],
        }

    bench_dir = ctx.workspace / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = bench_dir / f"{name}-{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report
