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

- **I1 — The refusal gate is calibrated and conservative.** Today: cosine ≥ 0.64 on the
  dense leg (retuned 2026-07-29; the previous 0.55 was calibrated against scores an IVF_PQ
  index was distorting — see limitation 1) and **fittable per workspace** via
  `qj eval --calibrate` (max-margin midpoint over the org's eval pack). Hybrid fusion, rerankers,
  graph hops, alias expansion — all may change *what surfaces and in what order*, never *whether
  we claim knowledge*. The invariant is the *shape* of the gate, not the number: what must
  survive is that one dense cosine, measured on un-quantized vectors, decides grounded-vs-refuse.
- **I2 — Evidence is a first-class object.** Chunks carry uri/title/source; graph edges
  carry `evidence_doc_id`; conversation-extracted facts cite their conversation. Anything
  that can't name its evidence doesn't enter the answer path.
- **I3 — Deterministic before generative.** If structure is parseable (manifests, ticket
  keys, deploy dashboards), parse it. LLMs propose; validators dispose (see the triple
  vocabulary gate). Generative extraction is allowed only behind strict shape validation —
  since 2026-07-30 that gate is two-layer: the controlled type/relation vocabulary **and**
  per-relation domain/range signatures, so a proposal whose three words are each legal but
  whose combination is a category error (`environment: prod | owns | person: bob`) is dropped
  like any other invalid line. The deterministic extractors satisfy those signatures by
  construction — validators constrain what the LLM may add, never what structure already proves.
  The clearest demonstration of this invariant paying out arrived 2026-07-31: on a real
  service catalogue the LLM extracted **0 edges from 17 team pages**, because a table
  flattened to text carries no sentence relating anyone to anything, while the *same* pages'
  key-value prose extracted normally. Preserving the table's structure at extraction and
  reading it deterministically (`ingest/tables.py`) recovered all 17. Structure the model
  cannot read is not a prompting problem.

## 2. The stack as built (and why)

```
                        ┌──────────────────────────────────────────┐
 connectors + uploads ─► │ INGEST  extract text (Word/PPT/Excel/    │
 pull/push/live/        │         PDF/HTML) → normalize → dedupe →   │
 browser/scrape/drop-box│         chunk → embed → graph extract      │
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
                       graph_relations · graph_path · remember · live connector tools ·
                       ops tools
                        ▼
                SURFACES  SSE chat/UI · CLI · briefs · evals · exports
```

Key defended choices:

- **Hybrid with RRF, not score mixing.** Ranks, not scores, fuse (k=60): immune to scale
  mismatch between cosine and BM25, deterministic, tunable with two knobs. Sparse-only
  candidates get their cosine computed *afterwards* purely to face the gate (I1).
- **Reranker on by default, and it is still the dominant query cost.** A cross-encoder
  reorders the fused head only, but reads query+candidate together, so it costs a forward
  pass per candidate. Two measured rounds of right-sizing have cut it without costing any
  watched quality metric: depth 24 → 16 (S4, 2026-08-10, quality flat from 12 to 32) and
  fp32 → the **INT8 build** of the same model (S4a, 2026-08-11, −28.6% on the stage, faster
  on 32/32 queries, `qj eval --compare` delta 0.000). It remains ~65–76% of a query, and
  what is left is candidate **length** — the median live candidate fills 415 of the model's
  512 tokens and cost is quadratic in sequence length — which makes the next lever chunking
  (S4b/#2), not the reranker. Kept on because retrieval quality is the product, and because
  the same measurements show what it buys: MRR 0.775 without it against 0.925 with.
  `QJ_DISABLE_RERANKER=1` or `retrieval.reranker="none"` turns it off, and the 23MB model
  loads lazily on first use, so nothing is paid by a workspace that never searches.
- **The graph is relational, not a graph DB.** At org scale (10³–10⁵ entities) SQL with
  three indexed tables beats operating Neptune/Neo4j; BFS in Python over ≤10⁴ edges is
  microseconds. Revisit only past ~10⁷ edges (see CLOUD_ROADMAP Y3).
- **One neutral message format; providers convert at the wire.** Anthropic and Ollama are
  adapters; extended thinking, tool loops, and streaming live above them. Adding a
  provider is a wire-format exercise, not a rewrite.
- **The prompt prefix is treated as cacheable state.** Tool specs are name-sorted (byte-
  stable across requests/processes) and the Anthropic adapter places `cache_control`
  breakpoints (system block = tools+system; a moving breakpoint on the conversation tail,
  spaced inside the API's 20-block lookback), so rounds 2..N of a tool loop and follow-up
  turns re-read the prefix at ~0.1× input price. The same stable ordering feeds automatic
  prefix caching on OpenAI-compatible backends and local KV-cache reuse. `llm.prompt_cache`
  gates the explicit markers (ON). Both sides are observable at DEBUG: Anthropic
  `cache_read_input_tokens`, OpenAI-compatible `prompt_tokens_details.cached_tokens`.
  Measured as of 2026-07-31 via `qj bench --agent`, which reports a `cache_hit_rate` from
  `ChatResult.usage` — and the first live reading was **0%** on the litellm/gpt-oss backend
  (22.4k prompt tokens re-paid every round), so on that path this is currently a designed
  capability rather than a realised saving. Anthropic's side still needs an API key to verify.
- **Skills carry procedural knowledge, which retrieval cannot.** Memory answers *what the
  org knows*; a skill states *how the org works* — the query syntax for a log index, the
  workflow a release follows. That is a different kind of knowledge: it is not a passage to
  be retrieved and cited, it is an instruction to be followed, so grounding it against
  `min_score` would be a category error. Skills therefore sit **beside** retrieval rather
  than inside it, in the open Agent Skills format (a folder with `SKILL.md`, optional
  `references/` and `scripts/`) that Claude Code and GitHub Copilot also read, so an org's
  existing skills work unmodified. Progressive disclosure is what makes a library
  affordable: only each skill's name and description ride the prompt, and the body is
  fetched when the model decides it applies. The multi-user consequence is the interesting
  one — a skill that *reaches* a real system runs with **the asking person's own
  credentials and never the server's**, so the same skill is a different capability for
  each user, and one whose values are missing is listed as unavailable with the missing
  names stated rather than hidden.
- **Evals in the repo.** Retrieval metrics (recall@k, MRR, grounded-recall, refusal
  accuracy) are deterministic and LLM-free, so quality is CI-checkable — the control
  system for every enhancement below (nothing merges without an eval gate).

## 3. Known limitations (the honest list the roadmap must fix)

Each maps to a pending item in `docs/AI_ROADMAP.md` (noted in parentheses).

1. Single embedding model (bge-small), and **it cannot cleanly separate refusals from
   answers by score alone**. Measured 2026-07-29 on a 32-case pack against a real org corpus
   (12 refusal cases, exact cosines): topically-adjacent non-answers score 0.62–0.78 while
   true hits score 0.66–0.87 — the ranges *overlap*, so no threshold is simultaneously
   refusal-safe and recall-safe. `qj eval --calibrate` fits the best available compromise
   (0.72 max-margin; 0.64 shipped as the recall-preserving choice), but the overlap itself is
   the limitation, and it is now the load-bearing argument for an embedding change rather than
   the recall argument that preceded it (#9, X1, X2, #14). Historical note: an earlier version
   of this list cited a "0.523-vs-0.55" margin and a specific sub-gate case as the signal —
   both were artifacts of the IVF_PQ defect (LanceDB's default index is product-quantized),
   fixed 2026-07-29 via `retrieval.ann_refine_factor`. Re-measured on the live corpus
   2026-07-30, that defect has two faces — distorted scores on the right chunk (observed
   2026-07-28) and, on another index instance, the right chunks missing entirely
   (recall@5 45% → 100% with the fix). Both are fixed; the overlap is real and is what
   remains. (A retuned default used to reach only *new* workspaces — `save_config`
   materialized every field, pinning it — so this threshold sat unapplied on the live corpus
   for two days. Fixed 2026-07-31: the settings blob is now stored sparsely, with a logged
   one-time adoption of superseded defaults. See CLAUDE.md's `memory/` bullet.)
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
8. **Query latency, per-answer cost and ingest throughput are all measured now.**
   `qj bench` times every retrieval stage, reads ingest docs/min from real run history
   (2026-08-04), and with `--agent` reports answer latency and tokens per answer. What it
   revealed on the live corpus is itself a limitation worth stating: the cross-encoder is
   **~65–76% of a query** even after two rounds of measured right-sizing (S4 depth 24 → 16,
   S4a fp32 → INT8), and an answer costs **22.4k tokens over 3 model rounds with zero
   prompt-cache reads** on the current backend (S5). A second limitation surfaced while
   measuring: this machine drifts materially under sustained ONNX load — an untouched
   embedder moved 25–64% between runs — so **cross-run bench comparisons are unreliable
   here and latency A/Bs need a control variable and interleaved arms** to mean anything.
9. Document ingestion is **text-first**: Word/PPT/Excel/PDF/HTML extract their text layer, but
   **images inside documents and scanned/image-only PDFs are not read** — the `ingest/extract.py`
   `ImageHandler` seam is wired for it, awaiting the vision layer (#23, plan 07).
10. **A skill's scripts are not sandboxed.** They run as subprocesses with the server's own
    privileges — confined to the skill folder, given no shell, bounded by a timeout, and
    handed only the calling user's resolved credentials, but able to do anything that OS
    user can. This is why installing a skill is an admin-only act while using one is open to
    everybody, and it is stated rather than mitigated: real isolation means a container or a
    WASM runtime per script, which is not built. Relatedly, `run_skill_script` has been
    exercised against tests and a synthetic script but **not yet end-to-end against a real
    credentialed system**.

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
