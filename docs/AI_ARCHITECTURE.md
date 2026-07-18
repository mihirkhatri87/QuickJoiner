# QuickJoiner — AI Architecture: Thought Process & Invariants

Authors: RAG/LLM engineering. 2026-07-11; restructured 2026-07-17.
Scope: why the AI stack is shaped the way it is — invariants, the stack as built, honest
limitations, and model strategy. **All forward-looking work lives in `docs/AI_ROADMAP.md`**
(quality tiers, the speed/cost S-track, the original-research X-track, and the recurring
frontier process); this doc describes only what exists and why.

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

Each maps to a pending item in `docs/AI_ROADMAP.md` (noted in parentheses).

1. Single embedding model (bge-small). The threshold is no longer only hand-tuned —
   `qj eval --calibrate` now fits it per corpus — but the 0.523-vs-0.55 refusal margin is
   still thin on large corpora until a calibration set is authored and applied (X1, X2, #14).
2. Chunking is format-aware but not *meaning*-aware; no doc-level context in chunks; code
   chunking is line-based, not AST-based (#2, #10).
3. No temporal model: stale evidence ranks equal to fresh; no as-of queries (#11, #5).
4. No learned feedback loop: clicks/confirmations/evals don't yet tune anything (#19, #9).
5. Compound questions rely on the agent choosing to multi-query; no principled
   decomposition (#6).
6. Long-context "ragless" opportunities (small corpora fit in one prompt with caching)
   are unexploited (#15, #16, S5).
7. Graph is entity-level; no community/global summaries for "what is this org about?"
   corpus-level questions (#12).
8. **Nothing measures latency or cost** — no timing instrumentation anywhere in the answer
   or sync path; speed work is blind until the bench harness exists (S1).

## 4. Forward roadmap → `docs/AI_ROADMAP.md`

All pending work — the quality tiers (#2, #5–#22), the speed/cost **S-track**, the
original-research **X-track** (X1–X7), the agentic-layer items, release sequencing, and the
recurring **frontier process** (monthly scan, quarterly research spikes, per-change
eval/bench gates) — lives in `docs/AI_ROADMAP.md`, the single forward-looking AI document.
Shipped items graduate out of it into its Shipped ledger + the `CLAUDE.md` architecture
bullets (house rule). This doc stays descriptive: if a capability is described here, it
exists in the tree.

## 5. Model strategy

- Answering: provider-pluggable; default premium (Claude) with local fallback (Ollama).
- Embeddings: local-first (fastembed) forever an option; cloud tier may offer larger
  models per tenant; every swap re-runs calibration (`qj eval --calibrate`) — the gate is
  model-relative.
- Rerank/NLI/decomposition: small local ONNX models preferred; degrade gracefully keyless.
- **No foundation-model training.** Our data advantage is org-specific evals and structure,
  not tokens.

The bar for "top system in the world" is not a leaderboard score; it is: *every answer
provably supported, every refusal statistically calibrated, every gap actionable, on the
customer's own corpus, offline if they demand it.* Nothing on the roadmap trades that away.
