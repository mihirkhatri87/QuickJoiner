# QuickJoiner — AI Roadmap & Frontier Process

The single forward-looking AI document: every pending improvement to inference **quality**,
**speed**, and **cost**, the **original-research track**, and the **standing process** that
keeps this product at the frontier instead of decaying into last year's RAG stack.
Created 2026-07-17, absorbing the pending roadmap from `AI_ARCHITECTURE.md` §4–§7 (that doc
is now purely descriptive: invariants, stack-as-built, limitations, model strategy).

House rules apply (CLAUDE.md): this doc holds **only unbuilt work**. Shipped items graduate
into the Shipped ledger + `CLAUDE.md`/architecture docs in the same change that ships them.
An item listed under a tier below is genuinely unbuilt.

---

## 0. Operating principles (inherited, non-negotiable)

- **I1–I3 survive every item here** (see `AI_ARCHITECTURE.md` §1): the refusal gate stays
  calibrated and conservative; evidence stays first-class; deterministic beats generative
  wherever structure is parseable. Any roadmap item that would trade these away is rejected
  at intake, whatever its benchmark numbers.
- **Nothing ships without a measurement gate.** Quality changes gate on
  `qj eval --compare` (non-zero exit on >2-pt regression). Speed changes gate on the bench
  harness (S1 — which is why S1 ships first). "Felt faster" and "seemed smarter" are not
  evidence.
- **Local-first is a constraint, not a preference.** Every item must state its keyless/
  offline degradation path. Cloud-only accelerations go to the cloud tier, never the default.
- **Honesty about originality.** Items below are labeled: *adopt* (proven elsewhere, apply
  here), *adapt* (known technique, novel fit to our honesty/graph architecture), *original*
  (we believe no product in this category does it). Original claims get re-checked at each
  frontier scan — the market moves.

## 1. The three axes and where we stand (2026-07-17)

| Axis | Measured today? | Instruments | Biggest known gap |
|---|---|---|---|
| **Quality** (grounded, cited, multi-hop, honest refusal) | ✅ | `qj eval [--agent]`, `--calibrate`, `--compare`, hop_coverage | No per-claim verification at answer time (#13); no temporal model (#11) |
| **Speed** (latency to first token / full answer / sync) | ❌ **not at all** | none — no timing anywhere in the repo | Can't optimize what we can't see → S1 |
| **Cost** (tokens per answer, LLM calls per sync, index size) | ❌ | none | Same → S1 measures tokens/calls too |

## 2. Quality roadmap (absorbed from AI_ARCHITECTURE §4; numbering preserved)

### Shipped (graduated)
How each works is documented in `CLAUDE.md` — the source of truth for current behavior.
- **#1 Contextual chunk enrichment** — deterministic breadcrumb form (2026-07-13).
  Remaining upgrade folded into #10.
- **#3 Threshold calibration per workspace** — `qj eval --calibrate/--apply/--compare` (2026-07-17).
- **#4 Query expansion with org aliases** — `memory/expansion.py` (2026-07-17).
- **#24 Cross-source identity bridge (`same_as`)** — `ingest/bridges.py` +
  `catalog.refresh_same_as_bridges` (2026-07-21). Deterministic cross-type name/alias
  bridges (incl. the connectors' declared "also known as" field); traversed by
  `graph_path`/`graph_expand`; scored as its own lowest-tier `name-bridge` confidence
  class. Deliberately a bridge, not a merge. Fuzzy identity remains with the plan-06
  adjudicator.

### Tier 1 — highest leverage, low risk
2. **AST-aware code chunking** (tree-sitter) — *adopt*. Functions/classes as chunk units
   with imports+signature context; symbol manifest feeds the graph. Target: large uplift on
   code questions.
5. **Recency & authority priors** — *adopt*. Small rank features (doc age, source type
   weight, in-graph degree) applied at RRF fusion — never at the gate (I1). Kill-switch
   per workspace.
22. **Octopus variable-set → graph extraction** — *adapt*. The indirection resolver for
    `ingest/pubsub.py`'s honesty rule: when appsettings holds `#{Orders.Topic}`, the real
    channel/database name lives in the Octopus project's variable set — and the Octopus
    connector already knows the project (= the service entity). Ingest variable sets and emit
    `service --references--> topic:<value>` / `--stores_in--> datastore:<value>` edges for
    Topic/Queue/Database-named variables and connection-string values, evidence = the
    variable-set document. Closes the placeholder gap for Octopus-centric orgs (this org's
    live graph literally contains `#{azureusername}` placeholders today). Same conservative
    direction rules; per-environment variable scoping can carry the environment entity as
    detail. Extension of the same mechanism: Terraform state/vars, k8s ConfigMaps.
25. **Drain stale deferred graph work** — *adopt*. `graph_pending` rows are only retried when
    a later sync **re-yields that document** (`pipeline.py:158`). For connectors that ingest a
    moving window this never happens: the live workspace has **1,331 TFS documents** queued
    for LLM triple extraction whose work items have aged out of the per-team recent-sprint
    slice, so their relationships are permanently unmined. Add a pending-drain pass (a job on
    the `SyncManager` surface, so it streams logs + lands in the activity feed) that re-reads
    those documents' stored chunks and resolves their triples without a connector round-trip.
    Cheap, and it converts already-paid-for ingest into graph edges.
21. **Relation signatures (ontology-lite domain/range validation)** — *adapt*. A signature
    table over the existing triple vocab (`deploys: service|project → environment`,
    `owns: team|person → repo|service|project`, …) enforced in `parse_triples` alongside the
    type/rel checks — today the checks are independent, so a semantically impossible triple
    like `environment: prod | owns | person: bob` validates. Closes that hole (strengthens
    I3), and signature-violating edges from the deterministic extractors become a
    lower-confidence signal for plan-06 scoring. Pure function, tiny effort, no schema change.
    This is the full extent of "ontology" we adopt eagerly — see the §5.2 intake-rejection
    note for what we deliberately do NOT build.
26. **ADO/TFS work-item hierarchy + link graph** — *adopt*. Today the Azure DevOps sync
    ingests each recent-sprint work item as a **flat** document (`azure_devops.py:21-38` —
    title/state/assignee/area/iteration/description only) with **no `$expand=relations`**, so:
    (a) the parent **Features/Epics** are lost — they carry no sprint iteration, so the
    `/iterations/{id}/workitems` pull (`:279`) never even returns them, and the code discards
    the `rel`/`source` of the relations it does get (`:285-289`); (b) every **work-item link**
    is lost — parent/child, Related, Predecessor/Successor, and the **development links** to
    PRs/commits/branches; (c) the pipeline's ticket-key regex matches Jira `PROJ-123` keys, not
    ADO `#id`, so ADO items get **no graph edges at all** — an asymmetry with the Jira connector,
    which asserts `ticket → part_of → project/epic`. So we hold ~sprint-scoped leaf stories with
    zero fabric between them or up to the features/epics they served. Fix, connector-local +
    deterministic:
    - fetch work items with `$expand=relations`;
    - walk **up** the hierarchy and ingest parent Features/Epics as their own documents (even
      though they sit outside the sprint window) so epic/feature context is in memory, not just
      leaf stories;
    - emit edges: `story --part_of--> feature`, `feature --part_of--> epic`,
      `story --references--> repo|pull_request|branch` (development links — strengthens the
      existing GitLab↔TFS build-map bridge), `story --related_to--> story`;
    - store the extra fields (parent id, story points, priority, acceptance criteria).

    **Incremental update / dedup — the load-bearing half (how already-synced stories gain the
    new edges without re-embedding).** The pipeline dedups on the document's *content* hash
    (`pipeline.py:170-176`): an unchanged story is `skipped` and its `_sync_graph` never re-runs,
    so shipping the extraction alone would leave every already-ingested story **edgeless** until
    its text next changes. Split the two concerns: keep `content_hash` gating chunk/embed
    (unchanged), and add a **graph-assertion signature** per document (hash of the computed
    entities+edges + an extractor-version stamp) so a doc whose *content* is unchanged but whose
    *graph output* differs re-runs only `_persist_graph` — cheap, no embedding, and
    `replace_doc_edges` is already idempotent per evidence doc. Then, on the first re-sync after
    this ships, every in-window story is re-yielded *with* relations, its content hash matches,
    its graph signature doesn't → **edges backfill with zero re-embedding**. Stories aged out of
    the recent-sprint window are re-hydrated by the **#25 drain pass** (re-reads stored chunks, no
    connector round-trip) rather than re-pulled — this item and #25 compose. Parent Feature/Epic
    docs are added once; their child `part_of` edges are re-asserted from each child's evidence
    doc every sync, so the hierarchy self-heals if a child moves. The extractor-version stamp is
    the general mechanism for *any* future connector whose graph extraction improves: bump it and
    the derived layer rebuilds on the next sync/drain without a re-embed. Optional refinement
    (ties to the ingest-vs-live-query balance): **skeleton-ingest + live-hydrate** — ingest only
    the cheap hierarchy skeleton (ids + parent links + titles) for correlation/graph and leave
    full per-field detail to the live `ado_query_work_items` tool, indexing the fabric without
    pulling every field of every item.

    Gate: connector unit tests (relations→edges, up-hierarchy walk, graph-signature backfill
    without re-embed), no `qj eval --compare` regression. Closes the Jira/ADO asymmetry and turns
    the sprint snapshot into a genuine feature→epic→story→PR fabric.

### Tier 2 — strong, moderate effort
6. **Query decomposition / multi-query** — *adopt*. LLM splits compound questions;
   retrieve per sub-query; RRF-fuse the union. Scripted-provider tests keep CI deterministic.
7. **Late-interaction reranking (ColBERT-small)** — *adopt*. Alternative second stage;
   benchmark against the fastembed CE on the eval pack before adopting.
8. **Learned sparse (SPLADE-class) leg** — *adopt*. Term-expansion sparse retrieval;
   heavier index cost — cloud tier first.
9. **Embedding fine-tune pipeline (W7.3)** — *adapt*. Contrastive pairs mined from evals +
   accepted answers; per-org LoRA adapters; auto-recalibrate the gate after every swap
   (#3 makes this safe). The compounding moat.
10. **Semantic + late chunking** — *adopt*. Embed long windows, pool to sub-chunks so
    vectors inherit document context; A/B against the shipped breadcrumb form.

### Tier 3 — differentiating, higher effort
11. **Temporal knowledge (PRD W6)** — *adapt*. Bi-temporal doc records; staleness-aware
    ranking; `as-of` filters; contradiction detection (conflicting same-entity claims
    surfaced with both citations, never silently merged — I2).
12. **Deterministic community summaries** (our answer to GraphRAG) — *adapt*. Cluster the
    evidence-weighted graph, generate cited per-community summaries, re-ingest as
    `graph-summary://` docs. LLM for prose, never for edges (I3).
13. **Corrective/self-checking retrieval loop** — *adapt*. After drafting, verify each
    citation supports its sentence (NLI entailment, small local ONNX model); failed claims
    re-retrieve or drop to refusal. Operationalizes I2 at answer time; pairs with X5.
14. **Conformal refusal** — *original* (in this product category). Replace the point
    threshold with conformal prediction over calibration sets → statistically guaranteed
    refusal error rates ("≤5% false-grounding at 90% coverage"). #3's calibrate flow +
    X1's self-mined packs provide the calibration data. A sentence no competitor says.
23. **Multimodal derive-to-text (vision/audio/video)** — *adapt*. Modality specialists
    (a vision model, whisper-class ASR) convert diagrams, screenshots, and recorded
    meetings into cited, provenance-stamped, confidence-discounted TEXT at ingest time and
    via a live `analyze_media` tool — the answering LLM never becomes multimodal, the
    grounding gate is untouched, and deterministic rungs run first (draw.io/SVG XML
    extraction with zero LLM calls, ffprobe metadata, phash frame dedupe, silence trim,
    sha-keyed derive-once cache). Provider-conditional capabilities (litellm vision+ASR on
    one proxy; ollama vision-native, ASR external/local; anthropic vision-native, ASR
    external). Full design: `docs/plans/07-multimodal-media.md`. Unlocks the knowledge
    class no text pipeline reaches: architecture diagrams and unwritten meetings.

### Tier 4 — the ragless track
15. **Cost/quality router** — *adapt*. Per query choose hybrid RAG / graph-first /
    full-context packing (corpora <~150k tokens, prompt-cached). Log every decision for eval.
16. **Cache-augmented generation** — *adopt*. Provider-side cached prefix of the stable
    corpus slice; re-warm on sync. Ragless where better, RAG where it scales.
17. **Hierarchical memory** — *adapt*. Episodic → semantic → structural → summary;
    retrieval walks down; the agent sees provenance level explicitly.

### Tier 5 — evaluation & learning flywheel
18. **Synthetic eval generation (W8)** — *adopt*, superseded in ambition by X1. Human
    approval gates; canary corpora with planted facts + planted absences per release.
19. **Online signals** — *adapt*. Citation clicks, 👍/👎, `remember` corrections, gap
    resolutions as weak labels feeding #9 and #3. Strict privacy defaults.
20. **Drift watch** — *adapt*, superseded in ambition by X2. Embedding-space + refusal-rate
    drift alarms; auto-suggest re-eval after a big sync.

### Agentic layer
- **Tool-use discipline evals**: scripted multi-hop scenarios (ticket→code→deploy) as
  agent-layer cases; measure hop completion, citation fidelity, `add_connector`
  confirmation compliance (the live-LLM gap in CLAUDE.md).
- **Planner/executor split** for long tasks: plan retrievals, execute in parallel,
  synthesize once — bounds token burn (pairs with S3).
- **Write-path tools** (Jira ticket from a gap, PR a doc fix) only after a safety review:
  dry-run previews, human confirm, audit log.
- **Small-model behavior tuning**: if local models underperform on tool discipline,
  behavior-tune a small Ollama model on our own tool traces — never a knowledge fine-tune.

## 3. Speed & cost roadmap (new axis — S-track)

S1 is the prerequisite for everything else in this section: no speed work merges without a
before/after bench table, exactly as no quality work merges without `--compare`.

- **S1 — `qj bench`: the latency/cost harness** — *adopt; build first*. Times every stage
  per query over a query pack: embed-query, dense leg, sparse leg, RRF fuse, rerank, graph
  expansion, gate decision; plus agent-layer first-token latency, full-answer latency, tool
  rounds, and token counts (prompt/completion per answer). p50/p95, JSON reports beside the
  eval reports, `--compare` with regression exit codes. Also benches sync throughput
  (docs/min, embed batch rate). Cheap to build: the stages already have clean seams
  (`store.search` legs in `hybrid.py`, reranker `rank()`, `graph_expand`, the agent loop).
- **S2 — ANN & vector economy at scale** — *adopt*. IVF tuning past `ann_min_rows`,
  scalar/binary quantization for large corpora, measured on S1 **and** the eval pack
  (gate: zero grounded-recall loss — a faster index that changes the gate's inputs is a
  correctness bug, not a win).
- **S3 — Parallelism in the hot path** — *adopt*. Dense + sparse legs concurrently;
  independent tool calls within one agent round concurrently; embed batches during sync
  pipelined with upserts. The agent loop is round-sequential today; multi-tool rounds are
  the cheap win.
- **S4 — Reranker right-sizing** — *adopt*. Auto-tune `rerank_candidates` depth from
  measured marginal gain (S1 × eval); evaluate smaller/quantized CE models; consider
  early-exit when fused-head order is already stable.
- **S5 — Prompt-cache-aware context assembly** — *adapt; partially shipped 2026-07-18*.
  SHIPPED (documented in CLAUDE.md `llm/` bullet): explicit Anthropic `cache_control`
  breakpoints (system block caches tools+system; moving message breakpoint + intermediate
  markers inside the 20-block lookback; `llm.prompt_cache` ON by default) and deterministic
  name-sorted tool specs in the agent — the byte-stable prefix that also feeds automatic
  prefix caching on OpenAI-compatible backends and Ollama/llama.cpp KV reuse. REMAINING:
  (a) measure the actual cost delta via S1 token counts once `qj bench` exists (incl. live
  `cache_read_input_tokens` verification — needs an Anthropic key); (b) a stable corpus
  digest / stable `extra_system` framing so caching survives session-summary refreshes;
  (c) keep volatile content (per-request scores, timestamps) after the last breakpoint as
  new prompt sections are added. (The narrow, cheap precursor to #16.)
- **S6 — GPU-accelerated, resource-aware parallel ingestion** — *adopt*. Make a large sync /
  full re-embed use the machine it runs on. Scope is honest: the only ingest stage that is
  local *compute* (not network pull or disk I/O) is **embedding** (`memory/embedder.py`
  `FastEmbedEmbedder`, ONNX), with the ANN index build and the reranker as secondary compute
  users; connector pull is network-bound and the LLM triple drain is network/Ollama-bound, so
  neither is a GPU target here (if the LLM is *local* Ollama it already uses the GPU itself, and
  then embedding contends with it for VRAM — deferred, see (4)). Small-org syncs are dominated
  by connector API paging, so this pays off at scale, on re-syncs, and on re-embeds — not on
  every sync. Four parts, phased:
  1. **Cross-document batch embedding + a machine profiler (CPU-first, no GPU, biggest ROI).**
     `store.upsert_document` embeds one document's chunks at a time today (`store.py:147`); ONNX
     — CPU *or* GPU — is far more efficient on large batches and GPUs starve on small ones.
     Accumulate chunks across documents into one embed call, and add a startup **profiler**
     (`os.cpu_count()`, RAM, and — via `onnxruntime.get_available_providers()` /
     `nvidia-smi`/NVML — GPU presence + VRAM) that emits an auto-tuned plan
     `{execution_provider, embed_batch_size, embed_workers, triple_workers}`, logged like the
     rest of the sync machinery. This establishes the batched-ingest integrity model (below) and
     speeds the default CPU path on its own.
  2. **GPU execution provider** behind an opt-in `[gpu]` extra (`fastembed-gpu` /
     `onnxruntime-gpu`, `CUDAExecutionProvider`), selected by the profiler. Runtime provider-probe
     with **graceful CPU fallback** — `fastembed-gpu` installed ≠ CUDA/cuDNN DLLs loadable, so a
     mismatched CUDA must silently degrade to CPU, never crash a sync. CUDA/cuDNN version-matching
     on Windows is the real cost/risk of this whole item; the default install stays pure-CPU.
  3. **Re-calibrate the grounding gate on GPU vectors.** GPU float math ≠ CPU bit-for-bit; bge
     vectors are near-identical but this is exactly the "embedding change ⇒ re-check `min_score`"
     rule (I1). Gate on `qj eval --compare` (zero grounded-recall loss, same bar as S2) and
     `qj eval --calibrate` before declaring done.
  4. *(Later, only if local Ollama)* VRAM-aware scheduling between embedding and a co-resident
     local LLM, so the two don't thrash the same GPU.

  **Integrity under batched/parallel ingest (the hard half — must not break pause/resume/stop).**
  Today's crash-safety rests on a document-serial invariant: per doc, vectors are written
  (`store.upsert_document`) *before* the content hash (`catalog.upsert_document`, the "done"
  marker), with `SyncControl.check()` between docs. Any batching/parallelism must preserve it:
  - **store-before-catalog per document, never reordered for speed** — a hash written before its
    vectors land means a kill loses those vectors but marks the doc "done", so it is silently
    missing until its text next changes.
  - **checkpoints land at batch boundaries**; a `SyncStopped` mid-batch commits only the
    fully-written docs, the rest stay un-hashed and re-ingest next run — safe because hash dedupe
    is idempotent.
  - the deferred **`graph_pending` drain** already survives interruption correctly — keep that seam.
  - useful parallelism given the GIL is a *staged pipeline* (chunk = Python/GIL → embed = native,
    releases GIL → upsert = I/O), not threaded pure-Python chunking; batching beats threading here.
    This is the ingest-side complement to **S3**'s "embed batches pipelined with upserts."

  Gate: **S1** (`qj bench` sync throughput — docs/min, embed batch rate — before/after) **and**
  the eval `--compare`/`--calibrate` recalibration above. Local-first by construction (it uses the
  user's own hardware); nothing leaves the machine. Feasibility note: the engineering weight is in
  the batched-ingest integrity model + the profiler/auto-tune + CUDA packaging, not in the GPU call
  itself.

## 4. Original research track (X-track)

Ideas we believe are **not implemented in any product of this kind**. Each runs as a
time-boxed spike with a pre-registered hypothesis and success metric (§5 process) — the
outcome is *ship* (graduate to a plan) or *archive with findings* (recorded below, so dead
ends stay dead). Originality labels re-checked at every frontier scan.

- **X1 — Self-calibrating workspace (eval self-mining)** — *original as a closed loop*.
  Auto-generate per-workspace eval packs from the corpus itself: questions synthesized from
  chunks with known provenance (answerable cases with ground-truth URIs), refusal probes
  synthesized from **gap clusters + coverage holes** (planted absences the corpus provably
  lacks). Feed the mined pack to `--calibrate` on a schedule → the refusal gate re-tunes
  itself as the corpus grows, no human eval authoring. Hypothesis: mined-pack calibration
  lands within ±0.03 of a human-authored pack's recommendation. Builds on #18/#3; the
  closed self-tuning loop is the novel part.
- **X2 — Honesty SLO monitoring (refusal-margin drift)** — *original as a product feature*.
  We already log best-score + near-misses on every refusal (`gaps` table) and scores on
  every answer. Track the two distributions over time; alert when the margin between
  grounded and refused thins (the measured 0.523-vs-0.55 leak risk, made continuous);
  auto-suggest recalibration (#3/X1) when a big sync shifts the distribution. "Your honesty
  gate is drifting" is an alert no competitor offers.
- **X3 — Evidence-linked answer cache** — *original combination*. Semantic answer cache
  (query-embedding keyed) whose entries are **invalidated through the knowledge graph**:
  each cached answer stores its cited doc ids; a sync that re-ingests/deletes any cited doc
  (or an edge it evidences) evicts the entry. Semantic caching is common; *provenance-true*
  caching that can never serve an answer whose evidence changed is not. Big speed/cost win
  on repeated onboarding questions with zero staleness risk.
- **X4 — Known-unknowns as first-class knowledge** — *original*. Promote gap clusters to
  graph nodes ("negative knowledge"): the agent can then answer "that is a *documented
  gap* — asked 12×, nearest evidence is Y, suggested source is Octopus, here's who resolved
  similar gaps" instead of a bare refusal. Refusals become navigable objects with history
  and remediation paths. Extends the gaps backlog + coverage-fog data we already have into
  the answer path itself.
- **X5 — Deterministic per-claim confidence bands** — *original*. Extend plan-06's
  server-side score ledger from per-candidate to per-sentence: each claim in an answer
  carries the deterministic confidence of the evidence chain behind it (evidence class ×
  corroboration — never LLM self-report), rendered as subtle bands in the UI. Pairs with
  #13: X5 displays confidence, #13 enforces entailment. Answer-level self-reported
  confidence exists in the market; deterministic evidence-derived per-claim bands do not.
- **X6 — Speculative agent loop with local draft** — *adapt, aggressive*. Draft tool-call
  decisions with the local small model (`qwen3:4b`), verify/execute with the configured
  model only when the draft is low-confidence or the tool is consequential. Hypothesis:
  ≥30% latency + cost reduction on multi-tool answers at unchanged eval-layer quality.
- **X7 — Workspace-adaptive ontology induction** — *original*. Today `parse_triples` drops
  every off-vocabulary relation — real org relationships (`monitors`, `alerts_on`,
  `escalates_to`, `migrates_to`) are discarded at ingest. Induce per-workspace vocabulary
  extensions: log dropped-but-well-formed proposals; when the same relation recurs across
  distinct evidence docs above a threshold, propose it for human approval (UI: one-click,
  like the gaps CTAs); approved relations join that workspace's validator (I3 preserved —
  the validator becomes extensible, never bypassed) and get a #21 signature at approval
  time. Each org's graph grows its own schema with zero up-front authoring — the
  anti-Palantir: their ontology takes a team; ours is induced and approved in one click.
  Hypothesis: on a connected org, ≥15% more edges serving cited answers and a measurable
  hop_coverage lift vs the fixed vocab, with zero garbage-edge regressions on the seeded
  eval pack.

### Research archive (dead ends, kept honestly)
*(empty — populated by spike outcomes; an archived idea lists the hypothesis, what was
measured, and why it lost, so it is never re-litigated from scratch)*

## 5. The process — how this stays cutting-edge without anyone remembering to make it so

Four loops at four cadences. Each is executable by pasting its prompt into a Claude Code
session on this repo (house style — same as the plan prompts). Outcomes land as edits to
THIS file (intake → tiers/S/X), new plans under `docs/plans/` (when something is promoted
to build), or archive entries (§4).

### 5.1 Per-change gate (already live)
Every retrieval/agent change: `qj eval --compare` vs the last report. Once S1 ships:
`qj bench --compare` too. Red gate = no merge. This is the floor, not the process.

### 5.2 Monthly — frontier scan (~1 session)
Survey what moved: new papers (retrieval, RAG evaluation, conformal methods, KG+LLM),
model releases (embedders, rerankers, small LLMs), and competitor/product changes in the
onboarding-intelligence / enterprise-RAG category. Score each candidate against the intake
rubric; append survivors to the tiers/S/X lists; re-verify §4's originality labels; note
anything that invalidates a current bet.

**Intake rubric** (all five must pass):
1. Compatible with I1–I3 (or explicitly strengthens one)?
2. Measurable on `qj eval` / `qj bench` with a pre-statable gate?
3. Local-first viable, or honestly scoped to the cloud tier?
4. Effort ≤ ~1 week to a testable slice (bigger → needs a plan first)?
5. Would a user of THIS product feel it (grounded quality, honesty, latency, cost) — not
   just a leaderboard?

**Paste-in prompt:**
```
Run the monthly AI frontier scan for QuickJoiner per docs/AI_ROADMAP.md §5.2. Read that
file and CLAUDE.md first. Survey (web search): (a) retrieval/RAG/graph-RAG/conformal
papers and benchmark results from the last ~6 weeks, (b) new embedding/reranker/small-LLM
releases usable local-first, (c) visible feature changes in enterprise-RAG/onboarding
products. For each candidate: score against the §5.2 intake rubric (all five must pass),
and state which axis (quality/speed/cost) and which existing item it affects. Then EDIT
docs/AI_ROADMAP.md: append accepted items to the right tier/S/X list with an originality
label, update any §4 originality labels the market has invalidated, and add a dated
scan-log line at the bottom of §5.2. Do not remove existing items. Present the diff and
a 5-line summary of what moved in the field.
```

Scan log: *(dated one-liners appended here by each scan)*
- 2026-07-17 — user-driven intake: "should we add ontology?" evaluated against the rubric →
  **#21 relation signatures** (Tier 1) and **X7 workspace-adaptive ontology induction**
  (X-track) accepted; full formal ontology rejected (below).
- 2026-07-17 — user-driven vocab curation (the manual fast-path of X7): types `topic` +
  `datastore`, rels `publishes_to`/`subscribes_to`/`stores_in` added to `triples.py` —
  runtime coupling (pub/sub, data residence) that manifests can't see. Shipped same day
  with lockstep prompt interpolation. When #21 lands, their signatures:
  `publishes_to/subscribes_to: service|project|repo → topic`,
  `stores_in: service|project|repo → datastore`.
- 2026-07-17 — follow-up shipped: **deterministic pub/sub + datastore extractor**
  (`ingest/pubsub.py`) — per-language Service Bus SDK patterns (C#/Python/JS/Java/Go), app
  config incl. Spring, connection strings, CFN/SAM/serverless templates; literals only,
  placeholders (`#{Var}`/`%VAR%`/`${VAR}`) skipped never guessed, direction asserted only
  when the API implies it. User intake from the same session: orgs hold real values in
  deployment tooling (Octopus vars, AWS CFTs) → CFT slice shipped as references-only;
  **#22 Octopus variable-set extraction** accepted into Tier 1 as the placeholder resolver.
- 2026-07-18 — user-driven intake: "can gemma's vision + whisper's audio derive context from
  other data types?" evaluated → **#23 multimodal derive-to-text** (Tier 3) accepted with the
  derive-to-text constraint (specialists at ingest/tool time, never a multimodal answering
  model); full plan authored as `docs/plans/07-multimodal-media.md`. Rejected in the same
  session: gemma-3-27b or whisper as *answering*-model replacements for gpt-oss-120b (vision
  model = weaker tool discipline; whisper = not a chat model at all).

- 2026-07-20 — **live-graph diagnosis** (user: "I've configured Confluence, Octopus, TFS and a
  repo but don't see many connected things"). Measured against the real AppRiver workspace
  rather than reasoned about: 14,172 edges / 3,580 entities across all four sources, but only
  **18 entities with evidence from more than one source** — the graph is four islands. Root
  cause: entities are keyed `type:name` and resolution buckets candidates by type, so the same
  thing is `service:connector` + `repo:connector` + a `pipeline:*` that builds it; 158 exact
  service↔repo name matches sit unmerged. → **#24 cross-source identity bridge (`same_as`)**
  accepted (Tier 1; chosen over cross-type *merging*, which would destroy a real distinction
  and be hard to unpick). Second finding: **1,331 TFS docs stuck in `graph_pending`** because
  deferred triple work is only retried when a sync re-yields the document, and ADO ingests a
  moving sprint window → **#25 drain stale deferred graph work** accepted (Tier 1). Third,
  frontend: the graph view's per-entity edge budget resolves to 5 edges/node at this scale
  (~2.8% of edges shown), which reads as an empty graph → density-budget item added to FE F0.

**Intake rejections** (don't re-propose without new evidence; mirrors the research archive):
- **Full formal ontology** (OWL/RDF class hierarchies, reasoners, triple stores, interop
  ontologies) — rejected 2026-07-17. (a) The useful inference — transitive reachability —
  already exists at query time via `graph_path`/`graph_expand` BFS *with per-hop evidence*;
  materialized inferred edges would carry no direct evidence doc (violates I2) and go stale.
  (b) Contradicts the defended relational-graph choice; reasoner machinery is oversized for
  10³–10⁵ entities. (c) Up-front ontology authoring is a category error for a product whose
  buyer is a new joiner on day 1 — that market (staffed ontology teams) belongs to Palantir
  and prices accordingly. (d) Interop ontologies (schema.org etc.) are irrelevant to
  org-internal tooling corpora. The accepted scope is exactly #21 + X7: validation
  signatures now, induced per-org vocabulary growth as research. Re-open only if evals on a
  connected org show multi-hop failures that BFS + confidence scoring provably cannot fix.

### 5.3 Quarterly — research spike (time-boxed, pre-registered)
Pick the top X-item by (expected differentiation ÷ effort). Before writing code, register
in the spike prompt: hypothesis, success metric + threshold, timebox (2–5 days), and the
kill criterion. Outcome is binary: **promote** (write `docs/plans/NN-…` via the plan
conventions and build it eval-gated) or **archive** (move to §4's archive with findings).
No zombie spikes: the timebox ends, the decision is made.

**Paste-in prompt:**
```
Run a QuickJoiner research spike per docs/AI_ROADMAP.md §5.3 on X-item: <Xn — name>.
Read AI_ROADMAP.md, AI_ARCHITECTURE.md (invariants), and CLAUDE.md first. PRE-REGISTER
before any code: hypothesis, success metric + numeric threshold, timebox, kill criterion —
write these into a spike file under docs/spikes/<date>-<xn>.md. Build the smallest
testable slice in a scratch workspace (never ~/.quickjoiner/default; venv python only;
FakeEmbedder for unit tests). Measure against the pre-registered metric using qj eval /
qj bench. Then decide: PROMOTE (author docs/plans/NN following the existing plan style,
update AI_ROADMAP.md moving the item from §4 to the target tier with a plan link) or
ARCHIVE (move the item to §4's research archive with hypothesis/measurement/why it lost).
Update the spike file with the outcome either way. Honest reporting: a null result
recorded well is a success of the process.
```

### 5.4 Continuous — self-measurement (automate when X1/X2 land)
Until X1/X2 exist this is a manual monthly step alongside the scan: run `qj eval` +
`--calibrate` on the live workspace's eval pack, note the margin between grounded and
refused score distributions, and record whether the recommendation drifted from the
applied threshold. Once X1 (self-mined packs) and X2 (margin monitoring) ship, this loop
runs itself and §5.4 collapses into an alert-driven check.

### Lifecycle (mirrors the CLAUDE.md house rules)
```
frontier scan → intake (tier/S/X list, this doc) → [research spike if X] →
promoted → docs/plans/NN (design+tests+prompt) → built, eval/bench-gated →
graduated: Shipped ledger here + CLAUDE.md/architecture docs; removed from pending lists
                                └→ archived with findings (X-items that lose)
```

## 6. Sequencing (updates the old §7; PRD release alignment)

- **R2 (now)**: #2 + #18/X1 slice + **S1** — "Prove it, and prove it fast": publish
  before/after eval *and* latency tables. S1 first — it is the measurement floor for
  everything after.
- **R3**: #5 #6 #19 + S3 S4 + X2 — feedback and monitoring light up with team usage.
- **R4**: #11 #12 #15 #16 + X3 — temporal + ragless + global questions; #14 conformal
  refusal as the headline trust feature (fed by X1's calibration data).
- **R5**: #13 + X5 answer-time verification default-on for enterprise; #7/#8 where latency
  budgets (measured, S1) allow; X4 turns the gap backlog into an answering capability.

---

The bar is unchanged from the architecture doc, extended by one clause: *every answer
provably supported, every refusal statistically calibrated, every gap actionable, on the
customer's own corpus, offline if they demand it — **and measurably fast enough that
nobody is tempted to trade the honesty for speed.***
