# Plan 05 — Evaluate retrieval & correlation on a connected org

> **STATUS: ⏸ DEFERRED (runbook, not yet executed).** This is an operational procedure, not a
> code change — the eval harness and both layers (`qj eval [--agent]`, `hops`/`hop_coverage`)
> already exist. The C0–C4 matrix has not been run on a real org; doing so unblocks the deferred
> embedding/fine-tuning decision. Needs an org-connected machine. See [STATUS.md](STATUS.md).

Goal: on a machine connected to real org systems, measure whether the retrieval/correlation
stack (contextual chunking, cross-encoder reranker, code-structural graph, LLM-extracted
relationships, graph-expansion retrieval) actually improves grounded, cited, multi-hop answers —
and decide which features to keep on and whether an embedding change is warranted. This is the
"decide with data" step that was deferred pending a real corpus.

## What's under test (and where each shows up)

| Feature | Config knob | Ingest-time? | Which eval layer reveals it |
|---|---|---|---|
| Hybrid dense+sparse | `retrieval.hybrid` | no (query-time) | retrieval + agent |
| Contextual chunking | `retrieval.contextual_chunks` | **yes — needs re-sync** | retrieval + agent |
| Cross-encoder reranker | `retrieval.reranker` (`fastembed`/`none`) | no (query-time) | retrieval + agent |
| Code-structural graph | always on at ingest | yes (already in corpus) | agent (via graph expansion) |
| LLM relationship triples | `graph.extract_triples` | **yes — needs re-sync + LLM** | agent (via graph expansion) |
| Graph-expansion retrieval | `retrieval.graph_expansion` | no (query-time) | **agent only** |
| Grounding threshold | `retrieval.min_score` (bge-tuned 0.55) | no | retrieval + agent |

## Methodology facts — get these right or the numbers lie

1. **Two layers.** `qj eval SET.yaml` runs the deterministic retrieval layer (`store.search()`):
   it reflects hybrid + contextual chunking + reranker, **but not graph expansion** (that lives in
   the agent's `search_memory`). `qj eval SET.yaml --agent` runs the real tool-calling agent
   end-to-end and **does** include graph expansion, LLM triples, and multi-hop chaining. So:
   - contextual chunking / reranker → judge on the **retrieval** layer (recall@k, MRR, grounded_recall).
   - graph expansion / triples / multi-hop → judge on the **agent** layer (hop_coverage, citation_rate).
2. **Ingest-time vs query-time toggles.** `contextual_chunks`, `graph.extract_triples`, and the
   embedding model change what is stored, so flipping them requires a **re-sync/re-embed**.
   `reranker`, `graph_expansion`, `hybrid`, `min_score`, `top_k` are query-time — just flip and
   re-run `qj eval` (each run builds a fresh context that reads current config).
3. **min_score is calibrated to bge-small (0.55).** Contextual chunking changes the embedded text,
   so watch `grounded_recall` and `refusal_accuracy` when it's on; if refusals start leaking or
   good answers get gated, re-tune `min_score` (try 0.50–0.62) and note it.
4. **Use a DEDICATED eval workspace** (`QJ_WORKSPACE=~/.quickjoiner/eval`), not production — the
   A/B re-embeds re-ingest the whole corpus and you don't want to thrash the live vector store.
5. **The reranker downloads ~80 MB on first real search.** Ensure network on first run and that
   `QJ_DISABLE_RERANKER` is **not** set in the environment.
6. **`--agent` costs real LLM calls** — one agent run per case, up to 10 tool rounds each. Budget
   for 30–50 cases × configs. Keep the case count sane on the first pass.

## Steps

1. **Eval workspace + real sources.** `qj init eval` (pick the provider), connect the same sources
   as production (`qj connect …` or the UI `/connect` wizard), `qj sync`, confirm with `qj status`
   and `qj sources`. Code sources (git/github) auto-build the code-structural graph on sync.
2. **Author 30–50 real eval cases** grounded in *this* org (see the paste-in prompt). Adapt
   `docs/evals/multi-hop-crosssource.yaml` — replace the placeholder repo/package/service/ticket
   names with real ones discovered from `qj sources`, the Waypoints graph (`GET /api/graph`), and
   sample `qj ask`. Include: single-source facts, multi-hop dependency (`repo → package → deploy`),
   ticket → code → deploy, symbol location, service dependency, and ~20% genuine refusal cases.
   Set `uris`/`keywords` to real strings and `hops` to the real source substrings each cross-source
   answer must touch.
3. **Run the config matrix** (minimize re-syncs):
   - **C0 baseline** — `contextual_chunks=off, reranker=none, hybrid=on, graph_expansion=off`.
     Sync once. `qj eval SET.yaml --agent`.
   - **C1 +reranker** (query-time) — `reranker=fastembed`. Re-run eval (no sync).
   - **C2 +graph expansion** (query-time) — `graph_expansion=on`. Re-run eval (no sync).
   - **C3 +contextual chunking** (ingest-time) — `contextual_chunks=on`, **`qj sync`**, re-run eval.
   - **C4 +LLM triples** (ingest-time, optional/expensive) — `graph.extract_triples=on`,
     **`qj sync`**, re-run eval.
4. **Collect + compare.** Every run writes `<workspace>/evals/<name>-<timestamp>.json`. Build a
   table of `config × metric` and a short recommendation.

## Deliverable

- A comparison table: rows = C0…C4, columns = retrieval `{recall_at_k, grounded_recall, mrr,
  hop_coverage, refusal_accuracy}` and agent `{citation_rate, keyword_coverage, hop_coverage,
  false_refusal_rate, refusal_accuracy}`.
- A recommendation: which features to leave on by default, any `min_score` re-tune, and whether
  the embedding model is the bottleneck (i.e., is an embedding swap / fine-tune now justified, per
  the deferred fine-tuning decision) or if chunking+reranker+graph already close the gap.

---

## Paste-in prompt (run in Claude Code on the org-connected machine)

```
Evaluate QuickJoiner's retrieval & correlation stack against this org's real connected data,
following docs/plans/05-eval-on-connected-org.md. Read that file and CLAUDE.md first.

RULES
- Use the project venv (never system python): e.g. .venv\Scripts\python.exe / .venv\Scripts\qj.exe
  (adapt to this machine). Reports save to <workspace>/evals/*.json.
- Work in a DEDICATED eval workspace: set QJ_WORKSPACE=~/.quickjoiner/eval (NOT production).
  A/B toggles re-embed the whole corpus.
- Ensure network is available (reranker downloads ~80MB on first search) and QJ_DISABLE_RERANKER
  is NOT set.
- Never write real secrets into config; connectors use env: indirection (token=env:VAR).

METHODOLOGY (do not get this wrong)
- Retrieval layer (`qj eval SET.yaml`) = store.search(): reflects hybrid + contextual chunking +
  reranker, NOT graph expansion. Agent layer (`qj eval SET.yaml --agent`, needs an LLM) = full
  stack incl. graph expansion + LLM triples + multi-hop chaining.
- Ingest-time toggles need a re-sync: retrieval.contextual_chunks, graph.extract_triples.
  Query-time toggles do NOT: retrieval.reranker, retrieval.graph_expansion, hybrid, min_score, top_k.
- To change config, load it, set it, save it, e.g.:
    from pathlib import Path
    from quickjoiner.app import build_context
    ctx = build_context(Path.home()/'.quickjoiner'/'eval')
    ctx.config.retrieval.contextual_chunks = False
    ctx.config.retrieval.reranker = 'none'
    ctx.config.retrieval.graph_expansion = False
    ctx.catalog.save_config(ctx.config); ctx.catalog.close()
  After changing an ingest-time toggle, run `qj sync` before the next eval.

STEPS
1. `qj init eval` (choose provider), connect the same sources as production, `qj sync`,
   verify with `qj status` / `qj sources`.
2. Author 30–50 eval cases in eval-set.yaml grounded in THIS org. Discover real names via
   `qj sources`, the knowledge graph (GET /api/graph or the Waypoints view), and a few `qj ask`
   probes. Adapt docs/evals/multi-hop-crosssource.yaml; replace all placeholder names with real
   repos/packages/services/tickets. Use the fields: uris (real uri/title substrings), keywords,
   hops (the real distinct sources a cross-source answer must touch), refusal:true for ~20% of
   cases that are genuinely NOT in the connected corpus. Show me the set for a sanity check before
   the full matrix run.
3. Run the matrix, capturing each run's JSON:
   C0 baseline (contextual off, reranker none, graph_expansion off) — sync once, `qj eval … --agent`
   C1 + reranker=fastembed (query-time)          — re-run, no sync
   C2 + graph_expansion=on (query-time)          — re-run, no sync
   C3 + contextual_chunks=on (ingest-time)       — `qj sync`, re-run
   C4 + graph.extract_triples=on (optional)      — `qj sync`, re-run
   If contextual chunking hurts grounded_recall/refusal_accuracy, sweep min_score 0.50/0.55/0.60
   (query-time) and report the best.
4. Parse the saved <workspace>/evals/*.json reports into ONE comparison table (rows C0..C4,
   columns = the retrieval + agent summary metrics). Then give a recommendation: which features to
   keep on by default, any min_score change, and whether the embedding model is the bottleneck
   (embedding swap/fine-tune justified) or if chunking+reranker+graph already close the gap.

DELIVERABLE: the comparison table + recommendation, plus the eval-set.yaml you authored (so it can
be committed and re-run over time). Report honestly — include cases that regressed and any config
that made things worse.
```
