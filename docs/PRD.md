# QuickJoiner — Product Requirements Document

Owner: Product (AI) · Contributors: RAG/LLM engineering ×2, AWS architecture, React engineering, UX
Version 1.0 — 2026-07-11 · Status: living document
Companion docs: `MARKET_ASSESSMENT.md`, `PITCH_DECK.md`, `AI_ARCHITECTURE.md`,
`FRONTEND_ROADMAP.md`, `TEST_STRATEGY.md`, `CLOUD_ROADMAP.md`, `design/`

---

## 1. Vision

Every senior engineer who joins a company spends 3–6 months rebuilding a mental model that
already exists — scattered across repos, tickets, wikis, pipelines, dashboards, and other
people's heads. QuickJoiner is an **onboarding intelligence system**: it connects to the
org's systems, learns them into a private memory, and answers only from what it has learned
— with citations — or says honestly "I haven't learned that yet."

North star: **cut time-to-productive for a senior hire from months to weeks**, measurably.

## 2. Personas

| Persona | Need | Success looks like |
|---|---|---|
| **P1 — New-joiner principal engineer** (primary) | Understand architecture, processes, roadmap, and people fast; find credibility-building quick wins | Ships a meaningful change in week 2; stops interrupting teammates for lookups |
| **P2 — Hiring manager / team lead** | Ramp visibility; reduce mentor drag | Sees coverage/ramp dashboards; mentors interrupted less |
| **P3 — Platform / DevEx team** | Deployable, governable knowledge tooling | One workspace per org; auth, sharing, audit |
| **P4 — Security / IT** | Data never leaves approved boundaries | Local-first or self-hosted; secrets via env indirection; auditable claims |

## 3. Problem statements

1. Org knowledge is fragmented across ≥6 system types; search is per-silo.
2. LLM assistants hallucinate org facts; trust collapses after the first invented URL.
3. Cross-system questions ("is PAY-123 implemented *and deployed*?") require tribal knowledge.
4. Cross-repo structure ("A consumes B's package") is machine-readable but never surfaced.
5. Orgs speak in shorthand ("nautical models") that literal search misses.
6. What the org *doesn't* know it doesn't know is invisible — knowledge debt is untracked.

## 4. Product principles

1. **Grounded or silent.** Every org-specific claim is cited or refused. Refusal is a feature.
2. **Evidence-native.** Answers, graph edges, briefs — everything traces to a document.
3. **Local-first, cloud-optional.** `pip install` works with zero infra; `DATABASE_URL` scales it.
4. **Deterministic before generative.** Parse manifests before asking an LLM to guess.
5. **Every connector, every mode.** Pull, push/webhook, live tools, authenticated browser, polite scrape — degrade gracefully down the ladder.
6. **Two dispatch modes, one service layer.** Slash commands (deterministic) and agent tools (conversational) hit identical functions.

## 5. Functional requirements — shipped epics

Story convention: `[S]` shipped (with the tests that pin it), `[W]` wishlist (§7).
ACs are Given/When/Then; tests name real files for shipped stories.

### E1 — Ingestion & memory
- **E1.1 [S]** As a joiner, I can `qj learn <path|url|"fact">` so ad-hoc knowledge enters memory.
  AC: folder → per-file docs; free text → taught note (`note://`); re-ingest of unchanged content is skipped (sha256 over normalized text). Tests: `test_pipeline.py`, `test_deps.py::test_pipeline_normalizes_and_dedupes_cosmetic_variants`.
- **E1.2 [S]** Content is normalized (NFKC, typographic folding) before hashing/chunking so cosmetic variants dedupe. Tests: `test_retrieval.py` (normalize suite).
- **E1.3 [S]** Chunking is content-aware (markdown/code/prose). Tests: `test_chunkers.py`.
- **E1.4 [S]** Dependency manifests (NuGet/Maven/Gradle/npm/Angular/PEP621/Poetry/Pipfile/setup/go.mod) produce a synthesized, alias-annotated dependency-map document per repo. Tests: `test_deps.py`.

### E2 — Connectors (13 types)
- **E2.1 [S]** files, git, github, gitlab, jira, confluence, azure_devops, octopus, grafana, datadog, dynatrace, elastic, web_scrape — each supporting the maximum feasible subset of PULL/PUSH/LIVE/BROWSER/SCRAPE. Tests: `test_connectors.py`, `test_phase4_connectors.py`, `test_scraper.py`.
- **E2.2 [S]** Webhook push ingestion with HMAC verification (`POST /hooks/<source>`). Tests: `test_api.py`.
- **E2.3 [S]** Scheduled syncs per source (`sync_interval_minutes`, APScheduler).
- **E2.4 [S]** Polite, resilient scraping: realistic headers, rate limiting, robots.txt, retry/backoff, headless-browser fallback on WAF blocks, `max_depth` link-hop cap. No CAPTCHA/IP/TLS evasion, ever. Tests: `test_scraper.py`.
- **E2.5 [S]** Secrets only via env indirection (`token=env:VAR`). Tests: `test_connectors.py::test_resolve_secret_precedence`.

### E3 — Grounded Q&A (the contract)
- **E3.1 [S]** Agent must search before answering org questions; cites `[title|uri]`; refuses below `retrieval.min_score` (cosine 0.55, bge-small-tuned). Tests: `test_agent_loop.py`, `test_evals.py`.
- **E3.2 [S]** Hybrid retrieval: dense + BM25 (FTS5 / Postgres tsvector) fused by RRF; optional cross-encoder reranker; **the refusal gate stays dense** so hybrid never converts a refusal into an invention. Tests: `test_retrieval.py`, `test_pg_backend.py`.
- **E3.3 [S]** Multi-hop chaining guidance (ticket → code → deploy) in the system prompt.
- **E3.4 [S]** Streaming + extended thinking on both providers (Anthropic, Ollama); SSE events `thinking|delta|tool_call|answer|error|done`. Tests: `test_streaming.py`, `test_api.py`.

### E4 — Knowledge graph (Phases A–C complete)
- **E4.1 [S]** `entities` / `entity_aliases` / `edges` tables in both backends; every edge carries `evidence_doc_id`; edge lifecycle follows its evidence document. Tests: `test_graph.py`, `test_pg_backend.py`.
- **E4.2 [S]** Deterministic extractors: dependency maps → provides/depends_on; ticket-key regex (stoplisted) → references; Jira → ticket part_of project/epic; Octopus → service deploys environment. Tests: `test_graph.py`.
- **E4.3 [S]** Org-alias resolution (`"nautical models"` → `package:appriver.nautical.models`).
- **E4.4 [S]** Agent tools `graph_neighbors` / `graph_path` (evidence per hop, honest NO_PATH); `GET /api/graph`, `GET /api/graph/path`. Tests: `test_graph.py`, `test_api.py`.
- **E4.5 [S]** Distill-time triples: conversation-extracted relationships, vocabulary-validated, evidence = the conversation document; keyless mode skips. Tests: `test_sessions.py`.
- **E4.6 [S]** Knowledge view: force-directed graph, type-colored, evidence panel, alias search. (Browser-verified; FE tests are wishlist → `TEST_STRATEGY.md`.)

### E5 — Sessions, projects, conversation memory
- **E5.1 [S]** Persistent sessions/projects; rolling-summary compression at user-turn boundaries; keyless digest fallback; tool-output truncation. Tests: `test_sessions.py`.
- **E5.2 [S]** Distill: summary + durable facts + relationship triples → searchable `conversation://` docs.

### E6 — Teach / act from any surface
- **E6.1 [S]** `POST /api/learn` + `/learn` composer command + `remember` agent tool + `qj learn` — one `teach_fact` service. Tests: `test_api.py`.
- **E6.2 [S]** `/scrape <url>` → SSE crawl → markdown+mermaid report → artifact modal → explicit Learn; post-scrape offer to persist as connector. Tests: `test_api.py`, `test_scrape_report.py`.
- **E6.3 [S]** `/connect` conversational wizard (frontend flow machine over the connector catalog).
- **E6.4 [S]** Agent-tool bridge: `scrape_website`, `list_connector_types`, `add_connector` (confirmation-gated, test-fail = not saved), `sync_source`. Tests: `test_ops_tools.py`.

### E7 — Briefs & exports
- **E7.1 [S]** `qj brief architecture|week1|roadmap|quick-wins` — cited, gap-listing, saved + re-ingested. Tests: `test_briefs.py`.
- **E7.2 [S]** Exports md/html/csv/pptx. Tests: `test_export.py`.

### E8 — Auth & sharing
- **E8.1 [S]** Opt-in local auth (open mode until first user); PBKDF2; hashed bearer tokens; connector ownership/sharing gates config+live tools, knowledge stays communal. Tests: `test_auth.py`.

### E9 — Deployment
- **E9.1 [S]** Docker single-container (volume-persisted); `docker-compose.cloud.yml` app+pgvector; backend selection purely on `DATABASE_URL`; SQLite↔PG parity pinned by tests. Tests: `test_pg_backend.py`, `test_factory.py`.

### E10 — Evals
- **E10.1 [S]** YAML eval sets; deterministic retrieval metrics (recall@k, MRR, grounded-recall, refusal accuracy) + agent-layer checks; JSON reports for before/after tuning. Tests: `test_evals.py`.

## 6. Non-functional requirements

| # | Requirement | Target | Verification |
|---|---|---|---|
| N1 | **Privacy** — local mode: no org data leaves the machine except explicit LLM calls; embedding local by default | Zero third-party calls with Ollama+fastembed | Network-audit test (wishlist), code review |
| N2 | **Grounding quality** | Refusal accuracy ≥ 0.95, grounded-precision ≥ 0.9 on org eval pack | `qj eval` in CI |
| N3 | **Retrieval latency** | P50 < 200 ms, P95 < 600 ms @ 100k chunks (local); P95 < 900 ms @ 5M (cloud) | Bench harness (wishlist W11.4) |
| N4 | **Chat first-token** | < 2.5 s P50 (provider-dependent; measure overhead ≤ 300 ms) | SSE timing test |
| N5 | **Ingest throughput** | ≥ 50 docs/s local (FakeEmbedder-normalized); embedding-bound otherwise | Bench harness |
| N6 | **Idempotency/crash-safety** | Re-running any sync never duplicates; partial sync resumable | Existing hash-dedupe tests + kill-during-sync test (wishlist) |
| N7 | **Backend parity** | Identical dense retrieval + graph semantics SQLite↔PG | `test_pg_backend.py` (env-gated, CI service container) |
| N8 | **Security** | No plaintext secrets at rest; tokens sha256; webhooks HMAC; UI masks secrets | `test_auth.py`, `test_api.py` |
| N9 | **Testability** | ≥ 90% line+branch coverage BE & FE; mutation score ≥ 60% BE core | `TEST_STRATEGY.md` |
| N10 | **Accessibility** | WCAG 2.2 AA on web UI | axe CI (FE roadmap P1) |
| N11 | **Observability** | Structured logs; OTel traces per request/sync (cloud) | Cloud roadmap Y1 |
| N12 | **Scale** | 1M chunks / 100k docs local; 50M chunks multi-tenant cloud | Cloud roadmap Y2–Y3 |
| N13 | **Portability** | Windows/macOS/Linux; Python 3.11+; no Docker required locally | CI matrix |
| N14 | **Cost ceiling** (cloud) | ≤ $0.15/user/day infra at 1k-user tenant | Cloud roadmap cost model |

## 7. Future wishlist — epics, stories, ACs, tests

### W1 — Ramp analytics & manager dashboard (P2's product)
- **W1.1** As a manager, I see a coverage map (sources connected, doc/chunk counts, graph density) and a ramp curve (questions asked, refusal rate over time) per joiner.
  AC: Given a workspace with ≥1 session, When I open /analytics, Then I see refusal-rate trend, top topics, coverage by source type; no message *content* is shown without the joiner's opt-in (privacy default: metadata only).
  Tests: API aggregation unit tests (counts by day), FE chart component tests, privacy test asserting content absent from payload.
- **W1.2** Weekly ramp digest email/Slack: what was learned, gaps hit, suggested next connections.
  AC: digest generated from eval + refusal logs; contains ≥3 actionable gap items; renders in Slack blocks and email HTML. Tests: golden-file digest snapshot; scheduler trigger test.

### W2 — Knowledge-debt backlog (turn refusals into work items) — ✅ DONE (2026-07-13, plan 01)
- **W2.1 ✅** Every refusal is logged with query, best score, and nearest sources.
  AC: refusal event persisted (query hash, score, timestamp); PII-free via `gaps.store_queries=false`; visible at /api/gaps. Tests: `test_gaps.py` capture cases + `test_api.py` contract. Fire-and-forget capture in `search_memory`.
- **W2.2 ✅** Gaps cluster into a ranked backlog ("12 questions about payments deploys — connect Octopus or teach the runbook").
  AC: embedding-cluster of refusal queries; each cluster names candidate connector types (query-term → connector-type heuristics in `gaps.TERM_HINTS`) + known-entity hints from the graph. Tests: deterministic clusters with FakeEmbedder; ranking-stability + threshold tests.
- **W2.3 ✅** One-click remediation: gap → prefilled /connect wizard or teach prompt.
  AC: `GapsPanel` CTAs — Connect deep-links into the /connect wizard at the name step with the type preselected (`startWizard(types, preselectType?)`); Teach prefills the composer; Dismiss/resolve marks the cluster resolved. Tests: resolution-state API test; wizard preselect unit; FE build.

### W3 — Team surfaces: Slack/Teams bot
- **W3.1** `/qj ask` in Slack answers with citations and a "not learned" state that links the gap backlog.
  AC: response ≤ Slack limits, citations as links, threads keep session context, auth maps Slack user → qj user. Tests: bot handler unit tests with recorded payloads; session-mapping test.

### W4 — IDE surface (VS Code)
- **W4.1** Sidebar Q&A grounded in workspace + org memory; "explain this repo's place in the org" uses graph_path from the open repo entity.
  AC: extension calls the same HTTP API; answers render citations as clickable URIs; works against localhost server. Tests: extension integration tests with a stub server.

### W5 — People & ownership graph
- **W5.1** Extract person/team entities from commits (authors), CODEOWNERS, Jira assignees → `person --works_on--> repo/service`, `team --owns--> x`, evidence-cited.
  AC: deterministic extractors only; opt-out config (`graph.people=false`); alias handling for name variants. Tests: extractor units per source; opt-out test; graph query test "who owns payments?".
- **W5.2** "Ask a human" fallback: refusals suggest the top-2 evidence-linked people.
  AC: suggestion cites why (edges), never guesses; absent people graph → no suggestion. Tests: suggestion ranking unit; refusal-integration test.

### W6 — Temporal knowledge & change awareness
- **W6.1** Bi-temporal doc records (valid-from/observed-at); answers can carry "as of <date>" and flag stale evidence (> N days).
  AC: staleness stamp on citations older than configurable horizon; `--as-of` retrieval filter. Tests: catalog schema migration test; retrieval filter test; stamp rendering test.
- **W6.2** "What changed since I last asked" daily orbit digest (per project).
  AC: diff computed from sync deltas + graph edge changes; cited. Tests: delta computation unit; digest golden file.

### W7 — Retrieval intelligence (see `AI_ROADMAP.md` for the full ladder)
- **W7.1 ✅** Contextual chunk enrichment — shipped 2026-07-13 as the deterministic breadcrumb
  form (`retrieval.contextual_chunks`, on by default; see the CLAUDE.md ingest bullet). The
  LLM-generated 1–2 sentence variant remains open (AI_ROADMAP #10 compares late chunking vs it).
- **W7.2** Query decomposition + multi-query retrieval for compound questions.
  AC: compound eval set answered with ≥2 sub-retrievals fused; no regression on simple set. Tests: decomposition unit (scripted LLM); eval gate.
- **W7.3** Embedding fine-tune pipeline from eval + click data; per-workspace threshold recalibration.
  AC: reproducible training job; calibrated threshold maintains refusal accuracy ≥ 0.95. Tests: calibration unit; eval gate.
- **W7.4** Ragless mode for small corpora: whole-memory context packing with prompt caching, auto-selected per query by a cost/quality router.
  AC: corpora < router threshold answer without vector search; identical citation format; router decision logged. Tests: router unit; parity eval RAG vs ragless.

### W8 — Auto-evals
- **W8.1** Synthetic eval generation from the corpus (Q from chunks, refusal probes from out-of-corpus topics), human-reviewable YAML.
  AC: `qj eval --init --synthetic N`; generated cases tagged; never auto-trusted for gates until approved. Tests: generator determinism with scripted provider; YAML schema test.

### W9 — Enterprise & multi-tenant (see `CLOUD_ROADMAP.md`)
- **W9.1** OIDC/SSO login; SCIM provisioning. AC/tests per cloud roadmap Y1–Y2.
- **W9.2** Tenant isolation with per-tenant encryption keys; audit log of every answer with its evidence set (compliance replay).
  AC: audit record = question, answer hash, evidence doc ids, model, timestamps; export to SIEM. Tests: audit completeness test on chat path.
- **W9.3** Knowledge scopes — personal-layer union (intake 2026-07-18; design in `CLOUD_ROADMAP.md` Y1 workstream 8). A user's `/learn` notes and private-source docs are visible only to them; retrieval/graph/suggest/gaps read the union of commons + own + shared via query-time source-visibility filtering; personal evidence never triggers org-entity merges; personal facts promote to org knowledge via review (metadata flip).
  AC: with auth on, user A's private note is never retrieved/cited/suggested for user B; graph paths and corroboration counts differ per user accordingly; promotion makes it commons; open mode byte-identical to today. Tests: per-user retrieval + graph filtering units, merge-guard unit, promotion round-trip, open-mode regression.

### W10 — UX North Star ("Orrery", see `design/`)
- **W10.1** Coverage fog-of-war layer on the Knowledge view (learned = lit, gaps = dark, refusal heat).
  AC: fog derives from source coverage + refusal clusters; clicking dark space opens remediation (W2.3). Tests: layer data-transform units; visual regression.
- **W10.2** Evidence-thread animation from answer → chunks → source; dossier pinning; ramp timeline ribbon.
  AC: thread renders for any cited answer; dossiers persist per project. Tests: FE component + interaction tests.

## 8. Release themes

- **R1 "Trustworthy core"** (done): grounding, hybrid retrieval, graph, surfaces, bridge.
- **R2 "Prove it"**: real-corpus evals, testability program (≥90%), W7.1, W8.1, live-LLM confirmation smoke.
- **R3 "Team product"**: W1, W2, W3, W5, cloud Y1.
- **R4 "Category maker"**: W6, W7.3/7.4, W10, cloud Y2.

## 9. Out of scope (explicit)

CAPTCHA/anti-bot evasion; scraping sites that refuse a real browser; writing to org systems
(creating tickets/PRs) until a dedicated safety review; training foundation models.

## 10. Open questions

1. Refusal telemetry default: opt-in vs opt-out for individual privacy (W1/W2 dependency).
2. People graph consent model per jurisdiction (W5).
3. Pricing seat vs workspace (see `PITCH_DECK.md` §Business model).
