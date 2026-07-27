# PRIORITIES — the one ordered backlog

The single cross-roadmap prioritization of **all pending work**, ordered by ROI
(value ÷ effort). This doc holds only: rank, one-line item, value, effort, and links back to
the source doc that owns the full design — go there for detail. **House rule (CLAUDE.md):
any change to a roadmap or plan — item added, removed, shipped, re-scoped — updates this
list in the same change.** A row here must always correspond to a live item in its source
doc; shipped work is deleted from here (the source docs' Shipped ledgers are the record).

Last reconciled against the roadmaps: **2026-07-26**.

**Value (V)** 1–5 — user value × differentiation × strategic leverage (5 = category-defining
or unblocks many other items; 1 = nice-to-have).
**Effort (E)** 1–5 — 1 ≈ <1 day · 2 ≈ 1–3d · 3 ≈ 3–6d · 4 ≈ 1–2 weeks · 5 ≈ >2 weeks.
**ROI = V/E**, the primary sort; ties broken by value, then by unblocking power.
Sources: [plans](plans/STATUS.md) · [AI_ROADMAP](AI_ROADMAP.md) · [FRONTEND_ROADMAP](FRONTEND_ROADMAP.md) ·
[CLOUD_ROADMAP](CLOUD_ROADMAP.md) · [PRD §7 wishlist](PRD.md) · [MARKET_ASSESSMENT](MARKET_ASSESSMENT.md).

## Now — highest ROI, start here

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 1 | Run the connected-org eval matrix (the "decide with data" runbook: hybrid/reranker/embedding decisions, calibration) — unblocks #9, #7, #10 and settles the standing fine-tuning question | 5 | 1 | 5.0 | [Plan 05](plans/05-eval-on-connected-org.md) |
| 2 | **Knowledge scopes — personal-layer union**: query-time per-user visibility over one store, ingest-time entity-merge guard, promotion flow. **Gates multi-user GA.** Raised from #14 on 2026-07-24 when the OneDrive connector shipped (a user can pull documents shared privately with *them* into a memory everyone can cite). **De-risked 2026-07-25**: the `SearchScope` pre-filter now filters both hybrid legs on both backends, so the retrieval half exists — what remains is deriving the scope from the acting user's ownership/sharing instead of a picker selection, plus the merge guard and promotion flow | 5 | 3 | 1.7 | [Cloud Y1.8](CLOUD_ROADMAP.md) · [PRD W9.3](PRD.md) |
| 3 | Drain stale deferred graph work (#25) — 1,331 live TFS docs are queued for triple extraction and will never be retried (their work items aged out of the sync window); converts already-paid-for ingest into edges | 3 | 1 | 3.0 | [AI #25](AI_ROADMAP.md) |
| 4 | Relation signatures (ontology-lite domain/range validation in `parse_triples`) — pure function, closes a real correctness hole, feeds plan-06 scoring | 3 | 1 | 3.0 | [AI #21](AI_ROADMAP.md) |
| 5 | Close plan 06 remainder (live entity-merge check on next clean re-sync; repo-graph refresh; connect Nautical+Stevedore) — retires a ◐ plan | 3 | 1 | 3.0 | [Plan 06](plans/06-multi-angle-confidence-scored-answers.md) |
| 6 | `qj bench` latency/cost harness (S1) — prerequisite for every speed item and the prompt-caching cost-delta measurement (S5 remainder) | 4 | 2 | 2.0 | [AI S1](AI_ROADMAP.md) |
| 7 | GraphView density budget — **server half only**: `/api/graph`'s 5-edge-per-node cap still delivers a dense graph as stubs; make the API limit adaptive and state what it elided. (Client half shipped 2026-07-24: viewport LOD + "N in view".) | 2 | 1 | 2.0 | [FE F0](FRONTEND_ROADMAP.md) |
| 8 | Slack + Teams connectors — the #1 catalog gap; first half of the W3 team surface | 5 | 3 | 1.7 | [Plan 03](plans/03-slack-teams-connectors.md) · [PRD W3](PRD.md) |
| 9 | Architectural-layer classification (#29) — deterministic layer tag (API/Service/Data/UI/Utility/Infra) from paths + import direction, never LLM; unblocks the guided tour's cross-repo spine (#17) and a readable layer-colored graph. *(Understand-Anything intake)* | 3 | 2 | 1.5 | [AI #29](AI_ROADMAP.md) · [FE F1](FRONTEND_ROADMAP.md) |
| 10 | Persona-adaptive answer detail (#30) — persona on the user profile shapes answer verbosity/framing (grounding gate untouched); chat header chip shows the active persona. *(Understand-Anything intake)* | 3 | 2 | 1.5 | [AI #30](AI_ROADMAP.md) · [FE F1](FRONTEND_ROADMAP.md) |
| 11 | S5 remainder: measure caching cost delta via S1; stable corpus digest / stable `extra_system` framing | 3 | 2 | 1.5 | [AI S5](AI_ROADMAP.md) |
| 12 | Octopus variable-set → graph extraction (#22) — resolves the `#{placeholder}` honesty gap on the live org's actual graph | 3 | 2 | 1.5 | [AI #22](AI_ROADMAP.md) |
| 13 | Recency & authority rank priors (#5) — small fusion-time features, never at the gate | 3 | 2 | 1.5 | [AI #5](AI_ROADMAP.md) |
| 14 | Hot-path parallelism (S3): dense+sparse legs concurrent; parallel tool calls in one agent round | 3 | 2 | 1.5 | [AI S3](AI_ROADMAP.md) |

## Next — strong value, real effort

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 15 | Coverage fog + remediation — the W10.1 dark map on GraphView; **stands up the first FE test harness** (banks F0's core) | 4 | 3 | 1.3 | [Plan 04](plans/04-coverage-fog.md) · [PRD W10.1](PRD.md) · [FE F0](FRONTEND_ROADMAP.md) |
| 16 | AST-aware code chunking (#2, tree-sitter) — the biggest single retrieval uplift for code questions; **also unblocks the function-level `calls` edges #18 blast-radius consumes** (externally validated by Understand-Anything's tree-sitter engine) | 4 | 3 | 1.3 | [AI #2](AI_ROADMAP.md) |
| 17 | Dependency-ordered guided tour (#28) — navigable "start here → next" onboarding walkthrough; entities sequenced by dependency depth, cross-repo via #8's layer spine + #24 `same_as` bridges; cited + refusal-safe like the briefs. The day-1 persona wedge. *(Understand-Anything intake)* | 4 | 3 | 1.3 | [AI #28](AI_ROADMAP.md) |
| 18 | Blast-radius / diff-impact (#27) — `graph_impact` tool + `GET /api/graph/impact` walk **reverse** edges (imports/calls/subscribes_to/deps) for a changed file/symbol/PR; cross-source + cited. Principal-engineer feature; sharper once #16's `calls` edges land. *(Understand-Anything intake)* | 4 | 3 | 1.3 | [AI #27](AI_ROADMAP.md) |
| 19 | People & ownership graph (W5): person/team entities from commits/CODEOWNERS/assignees + "ask a human" fallback on refusals — pure onboarding value | 4 | 3 | 1.3 | [PRD W5](PRD.md) |
| 20 | Natural-language self-control (`/qj`) + RBAC — full API control from chat via a permanent, non-ingested control connector; roles admin/editor/viewer with capability + connector scoping. **Code complete (stages 1–5); only live browser verification remains.** Prerequisite layer for multi-user (item 2) | 4 | 4 | 1.0 | [Plan 08](plans/08-self-control-api-and-rbac.md) |
| 21 | Multimodal derive-to-text (#23): vision/audio/video → cited text; diagrams + meeting recordings; A+B slice alone delivers "summarize this recording". **Document half de-risked (2026-07-22):** text-first Word/PPT/Excel/PDF/HTML ingestion + rolling Uploads connector shipped, and `ingest/extract.py` already threads an `ImageHandler` seam — reading embedded images / scanned PDFs is now wiring `vision.py` into `extract_text`. **Includes #23.a (added 2026-07-24): cover OneDrive/SharePoint images** — that store is full of whiteboard photos, screenshots and scanned PDFs, and the connector already reports them as a *not yet* rather than unsupported | 4 | 4 | 1.0 | [Plan 07](plans/07-multimodal-media.md) · [AI #23](AI_ROADMAP.md) |
| 22 | Reranker right-sizing (S4) — needs S1 first | 2 | 2 | 1.0 | [AI S4](AI_ROADMAP.md) |
| 23 | Query decomposition / multi-query (#6, W7.2) | 3 | 3 | 1.0 | [AI #6](AI_ROADMAP.md) · [PRD W7.2](PRD.md) |
| 24 | Tool-use discipline evals (agentic layer): scripted multi-hop scenarios, confirmation compliance — closes the standing live-LLM verification gap | 3 | 3 | 1.0 | [AI agentic](AI_ROADMAP.md) |
| 25 | Synthetic eval generation (#18/W8.1) — approval-gated; feeds calibration + canary corpora | 3 | 3 | 1.0 | [AI #18](AI_ROADMAP.md) · [PRD W8](PRD.md) |
| 26 | Conformal refusal (#14) — statistically guaranteed refusal error rates; the sentence no competitor says. After Plan 05 + #18 provide calibration data | 4 | 4 | 1.0 | [AI #14](AI_ROADMAP.md) |
| 27 | Ramp analytics & manager dashboard (W1) — P2's product; needs W2 (done) + FE harness (item 15) | 4 | 4 | 1.0 | [PRD W1](PRD.md) |

## Later — valuable, gated or heavy

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 28 | Semantic + late chunking (#10) — A/B against the shipped breadcrumb form (needs Plan 05 baseline) | 3 | 3 | 1.0 | [AI #10](AI_ROADMAP.md) |
| 29 | Recency/online-signal flywheel (#19) + drift watch (#20) — weak labels + alarms feeding #9/#3 | 3 | 3 | 1.0 | [AI #19/#20](AI_ROADMAP.md) |
| 30 | Cost/quality router + cache-augmented generation (#15+#16, W7.4) — the ragless track; leans on shipped prompt caching + S1 numbers | 3 | 4 | 0.8 | [AI Tier 4](AI_ROADMAP.md) · [PRD W7.4](PRD.md) |
| 31 | Corrective/self-checking retrieval loop (#13) — NLI citation verification at answer time | 3 | 4 | 0.8 | [AI #13](AI_ROADMAP.md) |
| 32 | Deterministic community summaries (#12) — corpus-level "what is this org about?" answers | 3 | 4 | 0.8 | [AI #12](AI_ROADMAP.md) |
| 33 | FE product hardening (F1): command-turn persistence, Storybook, a11y, palette | 3 | 4 | 0.8 | [FE F1](FRONTEND_ROADMAP.md) |
| 34 | Cloud Y1 "Team Server" epic: worker split, OIDC, S3 offload, secrets, OTel, IaC, DR (item 2 is its product-layer prerequisite) | 4 | 5 | 0.8 | [Cloud Y1](CLOUD_ROADMAP.md) · [PRD W9.1](PRD.md) |
| 35 | Temporal knowledge & change awareness (#11/W6): bi-temporal records, staleness, as-of, contradiction surfacing | 4 | 5 | 0.8 | [AI #11](AI_ROADMAP.md) · [PRD W6](PRD.md) |
| 36 | Embedding fine-tune pipeline (#9/W7.3) — the compounding moat; **gated on Plan 05 showing the gap** | 4 | 5 | 0.8 | [AI #9](AI_ROADMAP.md) · [PRD W7.3](PRD.md) |
| 37 | Late-interaction reranker (#7) / learned sparse leg (#8) — benchmark-gated alternatives, cloud-tier first | 2 | 3 | 0.7 | [AI #7/#8](AI_ROADMAP.md) |
| 38 | The Orrery incrementally (F2): fog GA, dossier mode, WebGL decision | 3 | 5 | 0.6 | [FE F2](FRONTEND_ROADMAP.md) · [PRD W10.2](PRD.md) |
| 39 | IDE surface (W4, VS Code) | 3 | 5 | 0.6 | [PRD W4](PRD.md) |
| 40 | Cloud Y2 multi-tenant SaaS (schema-per-tenant, embedding service, Bedrock path, SOC 2) | 3 | 5 | 0.6 | [Cloud Y2](CLOUD_ROADMAP.md) · [PRD W9.2](PRD.md) |
| 41 | Hierarchical memory (#17) | 2 | 4 | 0.5 | [AI #17](AI_ROADMAP.md) |
| 42 | GPU-accelerated, resource-aware parallel ingestion (S6) — **I/O half shipped 2026-07-22**: parallel file reads (files/git) + parallel Octopus releases + network page-prefetch across jira/github/gitlab/azure_devops/confluence. REMAINING: machine profiler + auto-tuned plan, cross-doc batch embedding (CPU too), opt-in `[gpu]` CUDA provider with fallback; batched-ingest integrity preserving pause/resume/stop. Pays off at scale / re-embeds; **gated on S1** + gate recalibration | 2 | 4 | 0.5 | [AI S6](AI_ROADMAP.md) |

## Research spikes (X-track — time-boxed, pre-registered, value uncertain by design)

Not ROI-ranked; these run on the quarterly spike cadence defined in
[AI_ROADMAP §4–5](AI_ROADMAP.md) with pre-registered hypotheses. Current queue: X1
(self-mined calibration packs) → X2 (drift) → X5 (claim-level verification) → X7
(workspace-adaptive ontology induction; #21 is its shipped fast-path) → X3 (evidence-linked
answer cache) → X4/X6.

## Standing rules

- Ordering is judgment applied to a rubric, not arithmetic worship — when two rows tie,
  prefer the one that unblocks more rows (that's why Plan 05 and S1 sit on top).
- Dependencies are stated inline ("needs", "gated on", "after") — never start a gated item
  before its gate.
- Cloud Y3–Y5 and the market-assessment connector long-tail are deliberately not ranked —
  too far out; they enter here when their year approaches or a customer forces them.
