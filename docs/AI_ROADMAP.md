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

## 1. The three axes and where we stand (2026-07-31)

| Axis | Measured today? | Instruments | Biggest known gap |
|---|---|---|---|
| **Quality** (grounded, cited, multi-hop, honest refusal) | ✅ | `qj eval [--agent]`, `--calibrate`, `--compare`, hop_coverage | No per-claim verification at answer time (#13); no temporal model (#11) |
| **Speed** (latency to first token / full answer / sync) | ✅ | `qj bench [--agent] --compare` — per-stage p50/p95, embedder chunks/sec, ingest docs/min | Rerank is 83% of a query (S4). Ingest throughput is read from real run history, so it needs runs in the window to report anything |
| **Cost** (tokens per answer, LLM calls per sync, index size) | ✅ **per answer** | `qj bench --agent` — tokens/answer + cache-hit rate via `ChatResult.usage` | 22.4k tokens/answer with 0 cache reads on the live backend (S5); index size still unmeasured (S2) |

The first bench run replaced two years of guessing with numbers, and immediately found a
defect no correctness test could see (a 36k-row table scan inside alias expansion, 27% of
query latency). That is the argument for the measurement-gate rule above, in one data point.

## 2. Quality roadmap (absorbed from AI_ARCHITECTURE §4; numbering preserved)

### Shipped (graduated)
How each works is documented in `CLAUDE.md` — the source of truth for current behavior.

- **Graph rebuild from ingested state** — `IngestPipeline.rebuild_graph`,
  `documents.graph_json`, `SyncManager.start_regraph`, `qj regraph`,
  `POST /api/graph/rebuild` (+ preview), connector-plate and Settings UI (2026-08-06).
  Re-runs the graph extractors over already-indexed text: no connector round-trip, no
  re-chunk, no re-embed, for when the extractors change but the documents do not.
  Measured before building: a naive replace-from-text rebuild would have destroyed **27%**
  of a real 100k-edge graph, because connector-supplied structural edges are computed during
  the fetch and none were persisted — hence the payload column and the reported
  faithful-vs-preserved split, which is never destructive.

- **Per-source breakdown — showing what each system separately asserts** —
  `agent/divergence.py`, `evidence_source_id` on `catalog._EDGE_SELECT`, `per <system>:`
  groups in `graph_relations`, plus retrieval-side score-ledger seeding (2026-08-05). The
  graph returns a **union**, so no single system necessarily claims the merged line and
  nothing revealed that. Two negative results worth keeping: the equivalent **retrieval-side**
  rule was built, measured at an **80% false-fire rate** on the real corpus, swept across
  thresholds, found to have **no separating setting**, and removed rather than tuned — several
  sources contributing to one answer is indistinguishable from several sources each answering;
  and comparing raw entity names flagged naming variants as conflicts, so names are folded
  before comparison. Final rates: 0–9% of groups by relation. The wording never claims a
  system is wrong, because differing coverage and genuine disagreement are indistinguishable
  here too.
- **Structured table extraction + the graph enumeration read** — `ingest/tables.py`,
  `extract.render_html_table`, `catalog.graph_relations` + the `graph_relations` agent tool
  (2026-07-31). Tables are preserved as markdown rows rather than flattened (a blank cell
  used to vanish and shift the row's remaining values into the wrong column) and mined
  deterministically: typed columns plus the entity a catalogue URL names itself by become
  edges, and a person row's email becomes an alias — the fix for a measured identity split
  where only 1 of 875 person entities was shared across sources. `graph_relations` answers
  "list all X with their Y", which neither existing graph tool nor top-k search could.
  Measured trigger: 0 of 17 team pages produced an edge, while the same pages' key-value
  blocks extracted fine.
- **S1 `qj bench` — the latency/cost harness** — `quickjoiner/bench/harness.py` (2026-07-31).
  Per-stage retrieval latency, embedder throughput, and (with `--agent`) answer latency +
  tokens per answer; `--compare` is the >20% relative regression gate. Seams added for it:
  `store.search(trace=)` on both backends and `ChatResult.usage`/`TokenUsage` on all three
  providers. Paid for itself on its first run — see the S-track baseline above and the
  36k-row entity table scan it exposed. **Completed 2026-08-04** with ingest throughput
  (docs/min overall and per connector), read from the real runs already recorded in
  `sync_events` rather than by performing a sync: running one measures the remote's mood on
  the day and a synthetic one measures a fixture, while the honest number was already on
  disk. Stopped and errored runs count — they ingested real documents over a real duration,
  and excluding them would systematically drop the long crawls whose throughput matters
  most — so `SyncManager` now records `ingested` on those paths too. A window with no
  finished run reports that, never a zero.
- **S4 (depth half) — reranker right-sizing** — `retrieval.rerank_candidates` 24 → 16
  (2026-08-10). Measured, not tuned: cross-encoder scores are independent per candidate, so
  the entire depth curve was simulated from ONE scoring pass over the live 57,659-chunk
  corpus's fused pools, after proving the reconstruction reproduces `store.search` exactly on
  32/32 eval queries. Quality is **flat from depth 12 to 32** (recall 0.950 / MRR 0.925 at 16,
  20, 24 and 32); the only structural feature is a cliff below 12, and it is one named case
  whose expected source sits at fused rank 12. 16 rather than the measured-best 12 is
  deliberate margin — too shallow costs recall, too deep costs only latency. Result on the
  live workspace: query p50 **1977 → 1401 ms (−29%)**, p95 −21%, with recall, grounded recall,
  MRR and hop coverage all unchanged. `qj eval --compare` does flag `refusal_accuracy`
  0.25 → 0.167: a single case that is correct at exactly depth 24 and wrong at 8, 10, 12, 16,
  20 **and** 32 — non-monotone, therefore not a property of depth, and recorded rather than
  tuned to. Also measured and rejected as levers: ONNX thread count (the default already beats
  every explicit setting) and batching (already one forward pass per depth). Details in
  CLAUDE.md's `memory/` retrieval bullet; the remaining levers are S4a above.
- **#31 Skip server error pages during a crawl** — `scraper.looks_like_error_page`
  (2026-08-04). Framework boilerplate markers AND brevity, so a genuine page *about* errors
  is not dropped; the crawl still follows a failed page's links (the page failed, the site
  did not) and reports the count it skipped, so an over-eager rule shows as a suspicious
  number rather than a thin corpus. Trigger: 363 of 728 documents in the live `web_scrape`
  corpus were the identical ASP.NET error page, ingested as answerable, citable content.
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
- **#25 Drain stale deferred graph work** — `IngestPipeline.drain_pending_graph` +
  `SyncManager.start_drain` + `GET /api/graph/pending` / `POST /api/graph/drain` +
  `qj drain-graph` (2026-07-30). Documents queued for LLM relationship extraction were only
  retried by a sync that re-yields them, which a moving-window connector never does — so they
  kept their chunks and citations and had **no edges at all** (`_sync_graph` defers a
  qualifying document's whole graph, deterministic assertions included). The drain re-reads
  the text actually indexed for them and finishes the job in place. Two honesty properties
  came out of building it: `graph_pending.graph_json` now carries the deterministic payload so
  a drain is a *faithful* rebuild rather than a re-derivation, and rows predating that column
  are counted and reported separately instead of inside a total that would read as full
  recovery. Full detail in `CLAUDE.md`'s pipeline + sync-manager bullets.
- **#21 Relation signatures (ontology-lite domain/range validation)** —
  `RELATION_SIGNATURES`/`signature_allows` in `ingest/triples.py`, enforced in
  `parse_triples` (2026-07-30). Each relation declares which entity types may stand on
  its left and right, so an LLM line whose three words are each in-vocabulary but whose
  combination is a category error (`environment: prod | owns | person: bob`) is dropped
  like any other off-vocabulary line. Deliberately permissive (rejects impossible shapes,
  not arguable ones) and rendered into both extraction prompts from the same table.
  `references` is explicitly unsigned — it asserts co-occurrence, not a typed link.
  **Calibrated against the live 109k-edge graph rather than authored from taste** — the
  reusable lesson: the first cut rejected 13.6% of in-vocabulary edges and **43% of those
  rejections were legitimate statements**; measuring showed genuine errors are overwhelmingly
  *domain* (wrong subject) errors, so subjects are constrained tightly and objects loosely.
  Final table rejects 8.3%, effectively all real category errors. A signature table is only
  honest if it is checked against a real corpus.
  The roadmap's secondary note ("signature-violating deterministic edges become a
  lower-confidence signal") turned out to be **inert by construction**: the deterministic
  extractors build their edges from structure, so every shape they emit conforms — pinned
  by a test rather than given a scoring penalty that could never fire. This is the full
  extent of ontology adopted eagerly; X7 remains the induction spike.
- **#26 ADO/TFS work-item hierarchy + link graph** — `hierarchy_graph`/`_merge_graphs` in
  `azure_devops.py` + a bounded hierarchy walk-up + per-document display metadata with a
  version-triggered backfill (2026-07-26). Closes the Jira/ADO asymmetry: `part_of`
  (Epic→Feature→Story/Bug→Task) and `related_to` edges, deterministic from TFS's own
  `System.LinkTypes.Hierarchy-Reverse`/`Related` relations. Also shipped in the same change,
  beyond the original item's scope: a document-browser tree view rendering the hierarchy with
  Team/Sprint metadata and ongoing-then-completed sort ordering. Full detail in `CLAUDE.md`'s
  `azure_devops.py` architecture bullet. (The original item's "graph-assertion signature"
  design was superseded by the simpler, already-existing `GRAPH_EXTRACTOR_VERSION` staleness
  mechanism — no new signature concept was needed.)

### Tier 1 — highest leverage, low risk
2. **AST-aware code chunking** (tree-sitter) — *adopt*. Functions/classes as chunk units
   with imports+signature context; symbol manifest feeds the graph. Target: large uplift on
   code questions. Externally validated by **Understand-Anything** (2026-07-23 scan), which
   runs a tree-sitter structural engine in production. Also the prerequisite for function-level
   `calls` edges — today `ingest/code_graph.py` extracts `defines`/`imports` by regex and
   punts call graphs as "need tree-sitter" (`code_graph.py:10`) — which #27's blast-radius
   analysis consumes at symbol granularity.
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
29. **Architectural-layer classification** — *adopt* (validated by **Understand-Anything**,
    which auto-groups nodes into API/Service/Data/UI/Utility). A deterministic pass tags each
    repo/module/symbol entity with an architectural **layer** (API / Service / Data / UI /
    Utility / Infra) from cheap, evidence-bearing signals — path segments (`/api`,
    `/controllers`, `/services`, `/repositories`, `/components`), filename conventions, and
    import direction from the existing `imports`/dependency-map edges — stored as an entity
    attribute, **never inferred by the LLM** (I3, keyless/offline). Pays off three ways:
    (a) GraphView groups/colors by layer so a dense multi-repo graph reads as an architecture,
    not a node soup (FE surface: FRONTEND_ROADMAP F1); (b) it gives the guided tour (#28) its
    **cross-repo spine** — order and cluster the walkthrough by layer, not just per-repo
    dependency depth (the user's note that layer classification helps resolve cross-repo
    relationships); (c) layer becomes a retrieval/answer signal ("where does auth live" →
    Service-layer entities first). Gate: extractor unit tests + no `qj eval --compare`
    regression (attribute-only, doesn't touch the gate).

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
30. **Persona-adaptive answer detail** — *adopt* (Understand-Anything adjusts density for
    junior dev / PM / power user). A **persona lives on the user profile** (new-joiner
    principal — the product default and wedge — / junior dev / PM / power user) and threads
    into `prompts.py` as an answer-**shaping** directive: verbosity, what to foreground (a PM
    wants flows and owners; a principal wants interfaces and failure modes; a junior wants more
    explained-not-assumed), and depth. **The grounding gate and citation contract are untouched
    (I1/I2): persona changes framing, never what counts as evidence or whether we refuse.**
    FE surface (FRONTEND_ROADMAP F1, the user's explicit ask): a persona picker in
    account/settings **and a small always-visible chip in the chat header naming the persona in
    effect**. Cheap, local-first (just prompt framing), and reinforces the persona-sharpness
    differentiator (`MARKET_ASSESSMENT.md`). Persona rides the chat request / `/api/settings`;
    gate: no `qj eval --compare` regression (behavioral framing only).

### Tier 3 — differentiating, higher effort
11. **Temporal knowledge (PRD W6)** — *adapt*. Bi-temporal doc records; staleness-aware
    ranking; `as-of` filters; contradiction detection (conflicting same-entity claims
    surfaced with both citations, never silently merged — I2).
    ⚠ **Not closed by the 2026-08-10 date work.** `agent/dates.py` resolves the dates in a
    *question* ("last Friday" → an exact UTC window) and grounds the prompts that judge
    recency. This item is about the dates on the *evidence* — when a document was valid vs
    when it was observed, ranking a stale claim below a fresh one, and answering "as of
    March". Query-side date handling is a prerequisite for `as-of` filters, not a partial
    delivery of them.
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
    **Seam already in place (2026-07-22):** text-first document ingestion shipped (Word/PPT/Excel/
    PDF/HTML via `ingest/extract.py` + the rolling Uploads connector), and its extractor already
    threads an optional `ImageHandler` that enumerates embedded images — so the *document* half of
    this item (embedded images, scanned PDFs) becomes wiring-only: build the handler from `vision.py`
    and pass it into `extract_text`. See plan 07 §0's shipped-seam note.
    **23.a — cover OneDrive/SharePoint images when vision lands (added 2026-07-24, user request).**
    The OneDrive connector reaches a document store that is full of images — whiteboard photos,
    screenshots pasted into a folder, scanned signed PDFs, exported diagrams. Today it refuses
    them *specifically* rather than generically: `extract.IMAGE_EXTENSIONS` names the formats,
    `extract_text` raises "image files can't be read yet — vision support is on the roadmap",
    and the connector's `skip_reason` reports them as a **not yet**, distinct from an
    unsupported type. That wording is a promise this item has to keep. Work when vision ships:
    (a) pass the vision `ImageHandler` through `OneDriveConnector._fetch_document`, so a
    standalone image learns like any other document; (b) same for images inside a learned
    `.zip` (`_extract_zip` already routes members back through `extract_text` and notes each
    skipped image); (c) drop the image branch in `skip_reason`; (d) re-learn is enough to pick
    them up — no schema change. Gate: an image learned via `/qj learn from this onedrive
    document <url>` produces a cited, confidence-discounted description, and refuses when the
    vision model returns nothing rather than ingesting an empty document.
27. **Blast-radius / diff-impact analysis** — *adopt* (Understand-Anything's `/understand-diff`:
    "which parts of the system your changes affect before you commit"). "What does this change
    touch?" We already store the edges (`defines`/`imports`/`publishes_to`/`subscribes_to`/
    `stores_in`/dependency-map); add a `graph_impact(files|symbols|pr)` agent tool +
    `GET /api/graph/impact` that walks **reverse** edges from the changed symbols/files to their
    dependents (who imports/calls/subscribes-to this), returning the affected entities with
    per-hop evidence and plan-06 confidence. **Cross-repo and cross-source** — a schema change's
    blast radius reaches the service that `subscribes_to` its topic, which a single-repo tool
    (UA) cannot see. Sharpest once #2's tree-sitter `calls` edges give function granularity, but
    useful today at module/dependency granularity. A principal-engineer feature; impact is only
    ever asserted along real, **cited** edges (I2 preserved — no invented reach). Gate:
    graph-tool unit tests (reverse-walk correctness, cross-source reach), no eval regression.
28. **Dependency-ordered guided tour** — *adapt* (Understand-Anything's `tour-builder`
    generates dependency-ordered walkthroughs; fit here to our multi-source graph). A new
    onboarding artifact beside `briefs.py`/`repo_docs.py`: a navigable "start here → next"
    walkthrough that **sequences entities by dependency depth** (topological order over
    `imports`/`depends_on`/dependency-map edges — foundations first, leaves last), each stop a
    **cited** mini-explanation built from real retrieved evidence (refuses to invent, like the
    existing briefs). Crosses repos via **#29's layer spine + #24's `same_as` bridges** rather
    than touring one repo in isolation — the multi-source graph is exactly what UA (single-repo)
    can't do, and directly serves the day-1 persona (`MARKET_ASSESSMENT.md` wedge). Saved +
    re-ingested like the other briefs (`tour://…`), surfaced as a `qj brief tour` type and a UI
    "Take the tour" entry. Gate: no `qj eval --compare` regression (reads graph + retrieval only).

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

**S1 shipped 2026-07-31** (`qj bench` — see the Shipped ledger and CLAUDE.md's
`bench/harness.py` bullet). Its rule now applies to everything below: no speed work merges
without a before/after `qj bench --compare` table, exactly as no quality work merges without
`qj eval --compare`. **Baseline on the live 57,659-chunk workspace** (32 queries × 3, after
S4's depth retune — the previous baseline was p50 1977ms / p95 2564ms at
`rerank_candidates=24`): retrieval **p50 1401ms / p95 2029ms**, of which rerank 76%, sparse 7%,
graph expansion 4%, dense 4%; embedder 117 chunks/sec; ingest 9.3 docs/min read from real run
history; agent layer (gpt-oss-120b via litellm) first token 22.3s, full answer 32.7s, 22.4k
tokens/answer over 3 rounds, **0 cache reads** (agent numbers unchanged since 2026-07-31 — no
agent-layer run has been made since). Beat those numbers or explain why not.

- **S2 — ANN & vector economy at scale** — *adopt*. IVF tuning past `ann_min_rows`,
  scalar/binary quantization for large corpora, measured on S1 **and** the eval pack
  (gate: zero grounded-recall loss — a faster index that changes the gate's inputs is a
  correctness bug, not a win).
- **S3 — Parallelism in the hot path** — *adopt*. Dense + sparse legs concurrently;
  independent tool calls within one agent round concurrently; embed batches during sync
  pipelined with upserts. The agent loop is round-sequential today; multi-tool rounds are
  the cheap win.
- **S4a — Smaller/quantized cross-encoder, and early exit** — *adopt; the remainder of S4
  after the depth half shipped 2026-08-10 (see the Shipped ledger)*. Depth is settled: quality
  is flat from 12 to 32, the default is now 16, and rerank is **76% of a 1.4s query** rather
  than 83% of a 2.0s one. What is left is the per-candidate cost itself, which measurement
  showed is linear in candidate **length** as well as count (4.3ms at 185 chars → 69.3ms at
  2789), and that **18% of live chunks already exceed the model's 512-token cap** and are
  silently truncated by the tokenizer. Three untried levers: a deliberate length cap (measured
  promising but its sign flipped with depth on a 20-answerable-case set — needs a bigger eval
  set to separate from noise, so it is gated behind #18/X1 synthetic eval generation), a
  smaller or quantized CE (`jina-reranker-v1-tiny-en`, INT8 ONNX), and early exit when the
  fused head order is already stable. Same gate as before: no change ships without
  `qj eval --compare` showing zero recall loss.
- **S5 — Prompt-cache-aware context assembly** — *adapt; partially shipped 2026-07-18*.
  SHIPPED (documented in CLAUDE.md `llm/` bullet): explicit Anthropic `cache_control`
  breakpoints (system block caches tools+system; moving message breakpoint + intermediate
  markers inside the 20-block lookback; `llm.prompt_cache` ON by default) and deterministic
  name-sorted tool specs in the agent — the byte-stable prefix that also feeds automatic
  prefix caching on OpenAI-compatible backends and Ollama/llama.cpp KV reuse. REMAINING:
  (a) ~~measure the cost delta via S1 token counts~~ **measurable as of 2026-07-31** and
  measured on the live litellm/gpt-oss workspace: **22.4k tokens per answer over 3 rounds
  with ZERO cache reads**, i.e. the whole prompt is re-paid every round on that backend.
  Whether that is the broker not caching or a prefix that isn't byte-stable is the next
  question, and `qj bench --agent`'s `cache_hit_rate` is how it gets answered. Anthropic
  `cache_read_input_tokens` verification still needs an API key (none on this machine).
  (b) a stable corpus digest / stable `extra_system` framing so caching survives
  session-summary refreshes;
  (c) keep volatile content (per-request scores, timestamps) after the last breakpoint as
  new prompt sections are added. (The narrow, cheap precursor to #16.)
- **S6 — GPU-accelerated, resource-aware parallel ingestion** — *adopt*. **Partially shipped
  2026-07-22 (the I/O half):** two reusable primitives now parallelize/overlap ingestion I/O —
  `files.read_documents_parallel` (bounded in-order parallel reads: `files`/`git` file trees +
  Octopus per-project releases; `QJ_READ_WORKERS`) and `connectors/util.prefetch_pages`
  (1-page-ahead network prefetch: jira/github/gitlab/azure_devops/confluence). Both pause/stop-safe
  and integrity-preserving (documented in CLAUDE.md's connector bullets). REMAINING (the compute
  half): batched cross-document embedding + GPU execution provider + machine profiler + gate
  recalibration described below. Make a large sync /
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
  with lockstep prompt interpolation. Their #21 signatures shipped 2026-07-30 as designed
  (with `package` added to each domain): `publishes_to`/`subscribes_to`:
  `package|project|repo|service → topic`; `stores_in`: `package|project|repo|service → datastore`.
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
- 2026-07-23 — user-driven intake: analysis of **Understand-Anything** (Egonex-AI — a
  single-repo, tree-sitter + multi-agent IDE plugin that builds a committable code
  knowledge-graph) against QuickJoiner. Most of its surface we already cover more strongly
  (multi-source connectors, grounded/refusal contract, hybrid retrieval + reranker,
  cross-source `same_as` bridges + runtime `pubsub` edges it can't see, live tools, RBAC).
  Four capabilities it specializes in passed the rubric and were accepted: **#27 blast-radius/
  diff-impact** (Tier 3), **#28 dependency-ordered guided tour** (Tier 3), **#29
  architectural-layer classification** (Tier 1), **#30 persona-adaptive answer detail**
  (Tier 2). Its tree-sitter engine is external validation for existing **#2** (noted there; it
  unblocks #27's function-level `calls` edges). **Noted-not-adopted** (model mismatch, not
  rubric failure): UA's commit-once portable-graph JSON artifact and offline-no-API-key
  dashboard are artifacts of being a local IDE plugin — QuickJoiner is a shared server with a
  live GraphView, so both are already served in a different shape. Off-mission for our
  onboarding wedge: wiki community-clustering and inline language-concept teaching.

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
