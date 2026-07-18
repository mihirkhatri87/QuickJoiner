# QuickJoiner — AI Architecture: Thought Process & Frontier Roadmap

Authors: RAG/LLM engineering. 2026-07-11.
Scope: why the AI stack is shaped the way it is, and the full ladder of enhancements to
stand with the best retrieval systems — RAG and ragless — in the world.

---

## 1. The governing thesis

**Trust is the product; retrieval is the mechanism.** Every design decision passes one
filter: *does this make an org-specific claim more provably right, or more honestly
refused?* This yields three architectural invariants that must survive every future
enhancement:

- **I1 — The refusal gate is calibrated and conservative.** Today: cosine ≥ 0.55 on the
  dense leg, empirically tuned per embedding model — and now **fittable per workspace** via
  `qj eval --calibrate` (max-margin midpoint over the org's eval pack). Hybrid fusion, rerankers,
  graph hops, alias expansion — all may change *what surfaces and in what order*, never *whether
  we claim knowledge*.
- **I2 — Evidence is a first-class object.** Chunks carry uri/title/source; graph edges
  carry `evidence_doc_id`; conversation-extracted facts cite their conversation. Anything
  that can't name its evidence doesn't enter the answer path.
- **I3 — Deterministic before generative.** If structure is parseable (manifests, ticket
  keys, deploy dashboards), parse it. LLMs propose; validators dispose (see the triple
  vocabulary gate). Generative extraction is allowed only behind strict shape validation.

## 2. The stack as built (and why)

```
                        ┌──────────────────────────────────────────┐
 connectors (13) ─────► │ INGEST  normalize → sha256 dedupe →      │
 pull/push/live/        │         content-aware chunk → embed      │
 browser/scrape         │         + graph extract (deterministic)  │
                        └───────────────┬──────────────────────────┘
                                        ▼
        ┌──────────────────── MEMORY (pluggable on DATABASE_URL) ─────────────────┐
        │ vectors: LanceDB ⇄ pgvector (HNSW)     sparse: FTS5 ⇄ tsvector+GIN      │
        │ catalog: SQLite ⇄ Postgres (one portable SQL base)                      │
        │ graph: entities / aliases / edges (evidence-carrying, doc-lifecycled)   │
        └───────────────┬──────────────────────────────────────────────────────---┘
                        ▼
                RETRIEVAL  dense + BM25 → RRF fuse → [optional cross-encoder]
                           → dense-gated refusal (I1) → scored, cited chunks
                        ▼
                AGENT  tool loop (≤10 rounds): search_memory · graph_neighbors ·
                       graph_path · remember · live connector tools · ops tools
                        ▼
                SURFACES  SSE chat/UI · CLI · briefs · evals · exports
```

Key defended choices:

- **Hybrid with RRF, not score mixing.** Ranks, not scores, fuse (k=60): immune to scale
  mismatch between cosine and BM25, deterministic, tunable with two knobs. Sparse-only
  candidates get their cosine computed *afterwards* purely to face the gate (I1).
- **Reranker off by default.** A cross-encoder is a quality lever with a latency/model-
  download cost; it reorders the fused head only. Local-first means opt-in heavyweight.
- **The graph is relational, not a graph DB.** At org scale (10³–10⁵ entities) SQL with
  three indexed tables beats operating Neptune/Neo4j; BFS in Python over ≤10⁴ edges is
  microseconds. Revisit only past ~10⁷ edges (see CLOUD_ROADMAP Y3).
- **One neutral message format; providers convert at the wire.** Anthropic and Ollama are
  adapters; extended thinking, tool loops, and streaming live above them. Adding a
  provider is a wire-format exercise, not a rewrite.
- **Evals in the repo.** Retrieval metrics (recall@k, MRR, grounded-recall, refusal
  accuracy) are deterministic and LLM-free, so quality is CI-checkable — the control
  system for every enhancement below (nothing merges without an eval gate).

## 3. Known limitations (the honest list the roadmap must fix)

1. Single embedding model (bge-small). The threshold is no longer only hand-tuned —
   `qj eval --calibrate` now fits it per corpus (Tier-1 #3, shipped) — but the 0.523-vs-0.55
   refusal margin is still thin on large corpora until a calibration set is authored and applied.
2. Chunking is format-aware but not *meaning*-aware; no doc-level context in chunks; code
   chunking is line-based, not AST-based.
3. No temporal model: stale evidence ranks equal to fresh; no as-of queries.
4. No learned feedback loop: clicks/confirmations/evals don't yet tune anything.
5. Compound questions rely on the agent choosing to multi-query; no principled decomposition.
6. Long-context "ragless" opportunities (small corpora fit in one prompt with caching)
   are unexploited.
7. Graph is entity-level; no community/global summaries for "what is this org about?"
   corpus-level questions.

## 4. Retrieval frontier roadmap (ordered by expected ROI ÷ risk)

Every item ships behind `qj eval` A/B gates on the standard pack + per-org packs. Uplift
numbers are targets to beat, not promises. Items graduate OUT of this roadmap into the
"Shipped" ledger below (and are documented as current behavior in `CLAUDE.md`) once built —
so a numbered item still listed under a Tier is genuinely unbuilt.

### Shipped (graduated from this roadmap)
Numbers are kept as anchors for the R1–R5 release refs; how each actually works is documented
in `CLAUDE.md` (memory/ingest/evals bullets) — the source of truth for current behavior.
- **#1 Contextual chunk enrichment** — the deterministic breadcrumb form
  (`pipeline.breadcrumb()`, `retrieval.contextual_chunks`). Remaining upgrade: the optional
  LLM 1–2 sentence context. (2026-07-13)
- **#3 Threshold calibration per workspace** — `qj eval --calibrate/--apply/--compare`
  (`evals/harness.calibrate`), max-margin midpoint over the org's eval pack. (2026-07-17)
- **#4 Query expansion with org aliases** — `memory/expansion.py`, `retrieval.alias_expansion`,
  the query-side twin of ingest aliasing. (2026-07-17)

### Tier 1 — highest leverage, low risk
1. → **shipped** (see ledger above).
2. **AST-aware code chunking** (tree-sitter): functions/classes as chunk units with
   imports+signature context; manifest of symbols per file feeds the graph too. Target:
   large uplift on code questions; enables "explain this function's role."
3. → **shipped** (see ledger above).
4. → **shipped** (see ledger above).
5. **Recency & authority priors**: small rank features (doc age, source type weight,
   in-graph degree) applied at fusion — never at the gate (I1). Kill-switch per workspace.

### Tier 2 — strong, moderate effort
6. **Query decomposition / multi-query**: LLM splits compound questions into sub-queries;
   retrieve per sub-query; fuse with RRF; agent answers over the union. Scripted-provider
   tests keep it deterministic in CI.
7. **Late-interaction reranking (ColBERT-small)** as an alternative second stage where the
   cross-encoder is too slow at depth; benchmark against fastembed CE on the eval pack.
8. **Learned sparse (SPLADE-class) leg** replacing/augmenting BM25: keeps exact-token
   virtues, adds term expansion; heavier index cost — cloud tier first.
9. **Embedding fine-tune pipeline (W7.3)**: contrastive training from (query, positive
   chunk) pairs mined from evals + accepted answers; per-org adapters (LoRA on the
   embedder); auto-recalibrate gate after every swap. This is the compounding moat.
10. **Semantic + late chunking**: embed long windows, pool to sub-chunks (late chunking)
    so chunk vectors inherit document context without generation; compare vs Tier-1 #1.

### Tier 3 — differentiating, higher effort
11. **Temporal knowledge (PRD W6)**: bi-temporal doc records; staleness-aware ranking
    feature; `as-of` filters; contradiction detection = same-entity edges/claims with
    conflicting values → surfaced, never silently merged (I2 demands both sides cited).
12. **Deterministic community summaries** (our answer to GraphRAG): cluster the *graph*
    (connected components / label propagation over evidence-weighted edges), then generate
    cited summaries per community from member docs; re-ingest as `graph-summary://` docs.
    Global "what is this org?" questions retrieve summaries; every sentence keeps
    citations. LLM used for prose, never for edges (I3).
13. **Corrective/self-checking retrieval loop**: after drafting, verify each citation
    actually supports its sentence (NLI-style entailment with a small local model); failed
    claims are re-retrieved or dropped to the refusal path. This operationalizes I2 at
    answer time and is the feature auditors will love.
14. **Conformal refusal**: replace a point threshold with conformal prediction over eval
    calibration sets → statistically guaranteed refusal error rates ("≤5% false-grounding
    at 90% coverage"). A sentence no competitor can currently say.

### Tier 4 — the ragless track (limitation 6)
15. **Cost/quality router**: per query, choose (a) hybrid RAG, (b) graph-first answering,
    (c) **full-context packing** — for corpora under ~150k tokens, pack normalized docs +
    graph digest into the prompt with provider prompt-caching; identical citation format.
    Router features: corpus size, question class, token budget, cache warmth. Log every
    decision for eval.
16. **Cache-augmented generation**: persistent provider-side cached prefix of the stable
    corpus slice (briefs, dependency maps, graph digest) so small-org answering approaches
    zero marginal retrieval — re-warm on sync. Ragless where it's *better*, RAG where it
    scales: the router makes it a continuum, not a religion.
17. **Hierarchical memory**: episodic (conversations) → semantic (distilled facts, already
    shipped) → structural (graph) → summary (communities). Retrieval walks down the
    hierarchy; the agent sees provenance level explicitly.

### Tier 5 — evaluation & learning flywheel (the control system for all of it)
18. **Synthetic eval generation (W8)** with human approval gates; canary corpora with
    planted facts + planted absences (refusal probes) regenerated per release.
19. **Online signals**: citation clicks, "learn this," corrections via `remember`, gap
    resolutions — logged as weak labels feeding #9 and #3. Strict privacy defaults.
20. **Drift watch**: embedding-space drift + refusal-rate drift alarms per workspace;
      auto-suggest re-eval when a big sync lands.

## 5. Agentic layer roadmap

- **Tool-use discipline evals**: scripted multi-hop scenarios (ticket→code→deploy) as
  agent-layer eval cases; measure hop completion, citation fidelity, confirmation
  compliance for `add_connector` (the live-LLM gap noted in CLAUDE.md).
- **Planner/executor split** for long tasks (brief generation over huge corpora):
  plan retrievals first, execute in parallel, synthesize once — bounds token burn.
- **Write-path tools** (create Jira ticket from a gap, PR a doc fix) only after a safety
  review: dry-run previews, human confirm, audit log — same confirmation grammar as
  `add_connector`.
- **Small-model behavior tuning** (from the fine-tuning decision memo): if local models
  underperform on tool discipline, behavior-tune a small Ollama model on our own tool
  traces — never a knowledge fine-tune (breaks freshness/citations/refusal).

## 6. Model strategy

- Answering: provider-pluggable; default premium (Claude) with local fallback (Ollama).
- Embeddings: local-first (fastembed) forever an option; cloud tier may offer larger
  models per tenant; every swap re-runs calibration (#3) — the gate is model-relative.
- Rerank/NLI/decomposition: small local ONNX models preferred; degrade gracefully keyless.
- **No foundation-model training.** Our data advantage is org-specific evals and structure,
  not tokens.

## 7. Sequencing (mirrors PRD releases)

- **R2**: #1 #2 #3 #4 (+ #18) — the "Prove it" release; publish before/after eval tables.
- **R3**: #5 #6 #9-v1 #19 — feedback loop lights up with team usage.
- **R4**: #11 #12 #15 #16 — temporal + ragless + global questions; conformal refusal (#14)
  as the headline trust feature.
- **R5**: #13 at answer-time default-on for enterprise; #7/#8 where latency budgets allow.

The bar for "top system in the world" is not a leaderboard score; it is: *every answer
provably supported, every refusal statistically calibrated, every gap actionable, on the
customer's own corpus, offline if they demand it.* Nothing on this roadmap trades that away.
