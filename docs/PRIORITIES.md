# PRIORITIES — the one ordered backlog

The single cross-roadmap prioritization of **all pending work**, ordered by ROI
(value ÷ effort). This doc holds only: rank, one-line item, value, effort, and links back to
the source doc that owns the full design — go there for detail. **House rule (CLAUDE.md):
any change to a roadmap or plan — item added, removed, shipped, re-scoped — updates this
list in the same change.** A row here must always correspond to a live item in its source
doc; shipped work is deleted from here (the source docs' Shipped ledgers are the record).

Last reconciled against the roadmaps: **2026-08-11** — **S4a shipped and its row is gone**
(the INT8 cross-encoder default: −28.6% on the rerank stage, faster on 32/32 queries, every
watched eval metric identical); everything below it moved up one. Its other two levers were
closed by measurement rather than built — early exit is impossible (reranking changes the
top-8 on 32/32 queries, so no stable head exists to exit on) and the length cap is
**provably** lossless only where it saves nothing — so what survives is a much smaller,
gated **S4b** (candidate length, i.e. chunking), ranked far lower at #24 and likely to be
answered upstream by #2 instead. Net: one row out, one weaker row in.

Prior: 2026-08-10 — two changes. **Knowledge scopes is done
and its row is gone**: the ingest-time merge guard and the promotion flow shipped, closing Cloud
Y1.8 and PRD W9.3, which were what gated multi-user GA; everything below it moved up one. And
**S4's depth half shipped**, rewritten in place as S4a: the reranker
is still the largest single cost in a query, just a smaller one, and the remaining levers are
different work from the depth decision that is now settled. Same pattern for **#29**, whose
classifier shipped the same day: row #4 becomes #29a (consume the layer), because measurement
showed the *classifying* was the small half and the payoffs it was ranked for — the tour spine
and the retrieval signal — are still entirely unbuilt.
Prior: 2026-08-09 (Agent Skills shipped — a new feature, so nothing graduated out; it added
one row, now #37, for the sandboxing it deliberately does not do).

**Value (V)** 1–5 — user value × differentiation × strategic leverage (5 = category-defining
or unblocks many other items; 1 = nice-to-have).
**Effort (E)** 1–5 — 1 ≈ <1 day · 2 ≈ 1–3d · 3 ≈ 3–6d · 4 ≈ 1–2 weeks · 5 ≈ >2 weeks.
**ROI = V/E**, the primary sort; ties broken by value, then by unblocking power.
Sources: [plans](plans/STATUS.md) · [AI_ROADMAP](AI_ROADMAP.md) · [FRONTEND_ROADMAP](FRONTEND_ROADMAP.md) ·
[CLOUD_ROADMAP](CLOUD_ROADMAP.md) · [PRD §7 wishlist](PRD.md) · [MARKET_ASSESSMENT](MARKET_ASSESSMENT.md).

## Now — highest ROI, start here

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 1 | Close plan 06 remainder — **user-gated, not dev work** (re-checked 2026-07-30): every open item (AC #1 worked example, AC #7 entity merge, §4 repo-graph refresh + connect Nautical↔Stevedore) needs the live org's credentials and a real sync; code + fixtures are done and green. Runs itself the next time those connectors sync — the merge now only needs an **ordinary** sync, not a clean one, for any doc below the current `GRAPH_EXTRACTOR_VERSION` | 3 | 1 | 3.0 | [Plan 06](plans/06-multi-angle-confidence-scored-answers.md) |
| 2 | Slack + Teams connectors — the #1 catalog gap; first half of the W3 team surface | 5 | 3 | 1.7 | [Plan 03](plans/03-slack-teams-connectors.md) · [PRD W3](PRD.md) |
| 3 | Layer as a retrieval and tour signal (#29a) — the **classifier shipped 2026-08-10** (`ingest/layers.py`, `entities.layer`, 54.8% of symbol entities tagged; GraphView can hide the 33.7%/16.0% that is test and vendored code). What is left is *consuming* it: order the guided tour (#12) by layer for its cross-repo spine, and make layer a retrieval signal ("where does auth live" → service layer first) — the latter gated on `qj eval --compare` showing no regression. Extending coverage past the ~60% of files whose path states a role needs import-direction inference, deliberately unbuilt because it infers rather than reads | 3 | 2 | 1.5 | [AI #29a](AI_ROADMAP.md) · [FE F1](FRONTEND_ROADMAP.md) |
| 4 | Persona-adaptive answer detail (#30) — persona on the user profile shapes answer verbosity/framing (grounding gate untouched); chat header chip shows the active persona. *(Understand-Anything intake)* | 3 | 2 | 1.5 | [AI #30](AI_ROADMAP.md) · [FE F1](FRONTEND_ROADMAP.md) |
| 5 | S5 remainder: **S1 measured 0 cache reads** on the live agent path, so the caching win is currently theoretical — find out why and fix it; stable corpus digest / stable `extra_system` framing | 3 | 2 | 1.5 | [AI S5](AI_ROADMAP.md) |
| 6 | Octopus variable-set → graph extraction (#22) — resolves the `#{placeholder}` honesty gap on the live org's actual graph | 3 | 2 | 1.5 | [AI #22](AI_ROADMAP.md) |
| 7 | Recency & authority rank priors (#5) — small fusion-time features, never at the gate | 3 | 2 | 1.5 | [AI #5](AI_ROADMAP.md) |
| 8 | Hot-path parallelism (S3): dense+sparse legs concurrent; parallel tool calls in one agent round | 3 | 2 | 1.5 | [AI S3](AI_ROADMAP.md) |

## Next — strong value, real effort

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 9 | Coverage fog + remediation — the W10.1 dark map on GraphView; **stands up the first FE test harness** (banks F0's core) | 4 | 3 | 1.3 | [Plan 04](plans/04-coverage-fog.md) · [PRD W10.1](PRD.md) · [FE F0](FRONTEND_ROADMAP.md) |
| 10 | AST-aware code chunking (#2, tree-sitter) — the biggest single retrieval uplift for code questions; **also unblocks the function-level `calls` edges blast-radius (AI #27) consumes** (externally validated by Understand-Anything's tree-sitter engine) | 4 | 3 | 1.3 | [AI #2](AI_ROADMAP.md) |
| 11 | Dependency-ordered guided tour (#28) — navigable "start here → next" onboarding walkthrough; entities sequenced by dependency depth, cross-repo via AI #29's layer spine + #24 `same_as` bridges; cited + refusal-safe like the briefs. The day-1 persona wedge. *(Understand-Anything intake)* | 4 | 3 | 1.3 | [AI #28](AI_ROADMAP.md) |
| 12 | Blast-radius / diff-impact (#27) — `graph_impact` tool + `GET /api/graph/impact` walk **reverse** edges (imports/calls/subscribes_to/deps) for a changed file/symbol/PR; cross-source + cited. Principal-engineer feature; sharper once AST chunking's `calls` edges land. *(Understand-Anything intake)* | 4 | 3 | 1.3 | [AI #27](AI_ROADMAP.md) |
| 13 | People & ownership graph (W5): person/team entities from commits/CODEOWNERS/assignees + "ask a human" fallback on refusals — pure onboarding value | 4 | 3 | 1.3 | [PRD W5](PRD.md) |
| 14 | Natural-language self-control (`/qj`) + RBAC — full API control from chat via a permanent, non-ingested control connector; roles admin/editor/viewer with capability + connector scoping. **Code complete (stages 1–5); only live browser verification remains.** The RBAC half is the access layer knowledge scopes (shipped 2026-08-10) sits beside — RBAC governs the API, scopes govern memory | 4 | 4 | 1.0 | [Plan 08](plans/08-self-control-api-and-rbac.md) |
| 15 | Multimodal derive-to-text (#23): vision/audio/video → cited text; diagrams + meeting recordings; A+B slice alone delivers "summarize this recording". **Document half de-risked (2026-07-22):** text-first Word/PPT/Excel/PDF/HTML ingestion + rolling Uploads connector shipped, and `ingest/extract.py` already threads an `ImageHandler` seam — reading embedded images / scanned PDFs is now wiring `vision.py` into `extract_text`. **Includes #23.a (added 2026-07-24): cover OneDrive/SharePoint images** — that store is full of whiteboard photos, screenshots and scanned PDFs, and the connector already reports them as a *not yet* rather than unsupported | 4 | 4 | 1.0 | [Plan 07](plans/07-multimodal-media.md) · [AI #23](AI_ROADMAP.md) |
| 16 | Query decomposition / multi-query (#6, W7.2) | 3 | 3 | 1.0 | [AI #6](AI_ROADMAP.md) · [PRD W7.2](PRD.md) |
| 17 | Tool-use discipline evals (agentic layer): scripted multi-hop scenarios, confirmation compliance — closes the standing live-LLM verification gap | 3 | 3 | 1.0 | [AI agentic](AI_ROADMAP.md) |
| 18 | Synthetic eval generation (#18/W8.1) — approval-gated; feeds calibration + canary corpora | 3 | 3 | 1.0 | [AI #18](AI_ROADMAP.md) · [PRD W8](PRD.md) |
| 19 | Conformal refusal (#14) — statistically guaranteed refusal error rates; the sentence no competitor says. After Plan 05 + #18 provide calibration data | 4 | 4 | 1.0 | [AI #14](AI_ROADMAP.md) |
| 20 | Ramp analytics & manager dashboard (W1) — P2's product; needs W2 (done) + FE harness (item 9) | 4 | 4 | 1.0 | [PRD W1](PRD.md) |

## Later — valuable, gated or heavy

| # | Item | V | E | ROI | Source |
|---|------|---|---|-----|--------|
| 21 | Semantic + late chunking (#10) — A/B against the shipped breadcrumb form (needs Plan 05 baseline) | 3 | 3 | 1.0 | [AI #10](AI_ROADMAP.md) |
| 22 | Recency/online-signal flywheel (#19) + drift watch (#20) — weak labels + alarms feeding #9/#3 | 3 | 3 | 1.0 | [AI #19/#20](AI_ROADMAP.md) |
| 23 | Cost/quality router + cache-augmented generation (#15+#16, W7.4) — the ragless track; leans on shipped prompt caching + S1 numbers | 3 | 4 | 0.8 | [AI Tier 4](AI_ROADMAP.md) · [PRD W7.4](PRD.md) |
| 24 | Candidate length / rerank length cap (S4b) — **gated on #18**. S4a closed the other two levers (INT8 CE shipped, early exit impossible); what is left is that per-candidate rerank cost is set by chunk length (median live candidate 1658 chars ≈ 415 of 512 tokens, cost quadratic in sequence length). A cap is provably lossless only above 2000 chars, where 4% of candidates sit, so it buys nothing measurable; every real saving is below the token cap and lossy (cap 600 = −55% for MRR 0.925 → 0.892) and undecidable on 20 answerable cases. May be answered upstream by #2 instead | 2 | 2 | 1.0 | [AI S4b](AI_ROADMAP.md) |
| 25 | Corrective/self-checking retrieval loop (#13) — NLI citation verification at answer time | 3 | 4 | 0.8 | [AI #13](AI_ROADMAP.md) |
| 26 | Deterministic community summaries (#12) — corpus-level "what is this org about?" answers | 3 | 4 | 0.8 | [AI #12](AI_ROADMAP.md) |
| 27 | FE product hardening (F1): command-turn persistence, Storybook, a11y, palette | 3 | 4 | 0.8 | [FE F1](FRONTEND_ROADMAP.md) |
| 28 | Cloud Y1 "Team Server" epic: worker split, OIDC, S3 offload, secrets, OTel, IaC, DR (its product-layer prerequisite, knowledge scopes, shipped 2026-08-10) | 4 | 5 | 0.8 | [Cloud Y1](CLOUD_ROADMAP.md) · [PRD W9.1](PRD.md) |
| 29 | Temporal knowledge & change awareness (#11/W6): bi-temporal records, staleness, as-of, contradiction surfacing. **Still fully open** — 2026-08-10 shipped query-side date resolution (`agent/dates.py`, `resolve_dates`), which dates the QUESTION; this item dates the EVIDENCE (valid-from/observed-at, staleness ranking, as-of). The former is a prerequisite for as-of filters, not a slice of them | 4 | 5 | 0.8 | [AI #11](AI_ROADMAP.md) · [PRD W6](PRD.md) |
| 30 | Embedding fine-tune pipeline (#9/W7.3) — the compounding moat; **re-measured 2026-07-29 on the expanded 12-refusal-case set** (was 4): confirms the **refusal discrimination** gap is real, not a measurement artifact — with the IVF_PQ fix applied (true cosines), refusal near-misses (0.62–0.78) and real answerable hits (0.66–0.87) overlap enough that `qj eval --calibrate`'s own max-margin threshold (0.72) still only reaches refusal_accuracy 0.917 at the cost of 3–4 of 20 answerable cases. Shipped `min_score=0.64` as a conservative interim (zero answerable-recall cost, refusal_accuracy 0→0.25) — not a fix, a stopgap. This is now the load-bearing argument for the embedding fine-tune, not the retired IVF_PQ-distortion one | 4 | 5 | 0.8 | [AI #9](AI_ROADMAP.md) · [PRD W7.3](PRD.md) |
| 31 | Late-interaction reranker (#7) / learned sparse leg (#8) — benchmark-gated alternatives, cloud-tier first | 2 | 3 | 0.7 | [AI #7/#8](AI_ROADMAP.md) |
| 32 | The Orrery incrementally (F2): fog GA, dossier mode, WebGL decision | 3 | 5 | 0.6 | [FE F2](FRONTEND_ROADMAP.md) · [PRD W10.2](PRD.md) |
| 33 | IDE surface (W4, VS Code) | 3 | 5 | 0.6 | [PRD W4](PRD.md) |
| 34 | Cloud Y2 multi-tenant SaaS (schema-per-tenant, embedding service, Bedrock path, SOC 2) | 3 | 5 | 0.6 | [Cloud Y2](CLOUD_ROADMAP.md) · [PRD W9.2](PRD.md) |
| 35 | Hierarchical memory (#17) | 2 | 4 | 0.5 | [AI #17](AI_ROADMAP.md) |
| 36 | GPU-accelerated, resource-aware parallel ingestion (S6) — **I/O half shipped 2026-07-22**: parallel file reads (files/git) + parallel Octopus releases + network page-prefetch across jira/github/gitlab/azure_devops/confluence. REMAINING: machine profiler + auto-tuned plan, cross-doc batch embedding (CPU too), opt-in `[gpu]` CUDA provider with fallback; batched-ingest integrity preserving pause/resume/stop. Pays off at scale / re-embeds; S1's gate is now in place (ingest docs/min is read from real run history), so a before/after claim is finally measurable — needs gate recalibration | 2 | 4 | 0.5 | [AI S6](AI_ROADMAP.md) |
| 37 | **Sandbox skill scripts** — `run_skill_script` (shipped 2026-08-09) executes a skill's bundled script as a subprocess with the SERVER's privileges: confined to the skill folder, no shell, timeout-bounded, and handed only the calling user's own resolved credentials, but able to do anything that OS user can. Installing is admin-only *because* of this, and the limitation is stated in three places rather than mitigated. Real isolation means a container or WASM runtime per script, plus a policy for what a skill may reach — genuinely heavy, and only worth it when skills are being installed by people who are not already trusted with the host. Gate: a multi-user deployment where admins ≠ operators | 2 | 5 | 0.4 | [AI_ARCHITECTURE §3.10](AI_ARCHITECTURE.md) |

## Research spikes (X-track — time-boxed, pre-registered, value uncertain by design)

Not ROI-ranked; these run on the quarterly spike cadence defined in
[AI_ROADMAP §4–5](AI_ROADMAP.md) with pre-registered hypotheses. Current queue: X1
(self-mined calibration packs) → X2 (drift) → X5 (claim-level verification) → X7
(workspace-adaptive ontology induction; #21 is its shipped fast-path) → X3 (evidence-linked
answer cache) → X4/X6.

## Standing rules

- Ordering is judgment applied to a rubric, not arithmetic worship — when two rows tie,
  prefer the one that unblocks more rows (that's why S1's measurement harness was taken
  ahead of the speed work it now gates, and why knowledge scopes was taken ahead of higher-
  ROI-on-paper rows until its enforcement core shipped).
- A row can be top-ranked and still not be *startable* here: #1 needs the user's own org
  credentials. Take the highest-ROI row that can actually be worked, and say which you
  skipped and why.
- Dependencies are stated inline ("needs", "gated on", "after") — never start a gated item
  before its gate.
- Cloud Y3–Y5 and the market-assessment connector long-tail are deliberately not ranked —
  too far out; they enter here when their year approaches or a customer forces them.
