# Plan 01 — Knowledge-Debt Backlog (PRD W2.1–W2.3)

Effort: ~4 dev-days · Dependencies: none · Unblocks: Plan 04 (fog), later W1 (analytics)

## 1. Design

**Thesis:** every refusal is a data point about what the org needs to learn. Capture it at
the exact moment it happens (`search_memory` → `NO_RESULTS`), cluster similar refusals,
attach a remediation suggestion, and expose one-click fixes.

### Data model (portable SQL — both backends automatically)
```sql
CREATE TABLE IF NOT EXISTS gaps (
    id TEXT PRIMARY KEY,              -- uuid4
    query TEXT NOT NULL DEFAULT '',   -- empty when gaps.store_queries=false
    query_hash TEXT NOT NULL,         -- sha256, always present (dedupe/count key)
    best_score REAL NOT NULL DEFAULT 0,
    nearest_json TEXT NOT NULL DEFAULT '[]',  -- top-3 near-miss {source_id,title,score}
    session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',      -- open | resolved
    resolution TEXT NOT NULL DEFAULT '',      -- connected:<name> | taught | dismissed
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gaps_status ON gaps(status);
```

### Capture point
`agent/tools.py::build_builtin_tools.search_memory` — the branch that returns the
`NO_RESULTS:` string. Log fire-and-forget (a gap-log failure must never break answering).
Near-misses: re-run `store.search(query, top_k=3, min_score=0.0)` for the below-gate hits
(cheap; reuses the query embedding path). Do **not** log from the evals harness (evals set
a flag) or from `/api/search` (browsing ≠ asking).

### Config
`GapsConfig` in `config.py`: `enabled: bool = True`, `store_queries: bool = True`
(hash-only when false — the shared/cloud privacy mode), `cluster_threshold: float = 0.8`.
Mount as `Config.gaps`.

### Clustering + remediation (`quickjoiner/gaps.py`, new module — pure logic)
- `cluster_gaps(gaps, embedder, threshold)` — greedy centroid clustering over query
  embeddings, first-fit by cosine ≥ threshold, deterministic given insertion order
  (sort by created_at). Hash-only gaps cluster by exact hash equality only.
- `suggest(cluster)` — term→connector-type hints table (deploy/release/rollback→octopus;
  ticket/sprint/epic→jira; wiki/runbook/doc→confluence; log/error/trace→grafana|datadog|
  elastic; pipeline/build→azure_devops|github; repo/code→git) + entity hints via
  `catalog.resolve_entity` over query n-grams (aliases already exist).
- Returns clusters sorted by size desc: `{id, label (medoid query), count, samples[≤3],
  suggested_connectors[], entity_hints[], gap_ids[]}`.

### API (auth like `/api/learn`)
- `GET /api/gaps` → `{open_count, clusters:[…]}` (clusters computed on read; corpus is
  small — no persistence of clusters, no staleness bugs).
- `POST /api/gaps/resolve` `{gap_ids:[…], resolution:str}` → marks resolved.

### Agent tool
`list_gaps()` in `build_builtin_tools` — "what don't you know yet?" returns top clusters +
suggestions. Refusals thereby become self-describing.

### Frontend
- `api.ts`: `gaps()`, `resolveGaps()`. Rail: "Knowledge gaps" row with open-count badge →
  opens `GapsPanel` (drawer-style, reuse panel tokens): cluster cards with count, samples,
  and CTA chips: **Connect <type>** (starts the `/connect` wizard with the type
  preselected — extend `startWizard(types, preselectType?)` to skip the type step) and
  **Teach** (sets composer input to `/learn ` and focuses it). Resolve/dismiss buttons call
  the API and refresh.

## 2. Acceptance criteria
1. A refused chat question creates exactly one open gap; a grounded answer creates none.
2. `store_queries=false` ⇒ `query` column empty everywhere, clustering degrades to
   hash-equality, API never returns query text.
3. Three same-topic refusals form one cluster with count 3 and a sensible suggestion.
4. Resolving removes from open clusters; `open_count` drops; audit fields set.
5. Gap-log failure (e.g. catalog error) never breaks `search_memory` (answer still returns).
6. Works identically on SQLite and Postgres (parity test).
7. UI: badge count matches API; Connect CTA lands in the wizard at the name step with the
   type chosen; Teach CTA prefills the composer.

## 3. Test matrix
Backend (`tests/test_gaps.py`): capture on NO_RESULTS / not on hit / not when disabled /
hash-only mode; near-miss payload shape; clustering determinism + threshold behavior +
hash-only clustering; suggestion table (one case per hint family); resolve lifecycle;
list_gaps tool formatting; exception-swallowing test (catalog method monkeypatched to
raise). API (`test_api.py`): GET/POST contract, auth-gated, privacy mode. PG
(`test_pg_backend.py`): gaps table round-trip (add table name to `_TABLES` reset list!).
FE: `wizard.ts` preselect unit; GapsPanel render + CTA callbacks (once the FE harness from
Plan 04 phase 0 exists — otherwise manual browser check and note it).

---

## 4. Implementation prompt (paste into a fresh Claude Code session)

```
Implement the Knowledge-Debt Backlog feature for QuickJoiner exactly per
docs/plans/01-knowledge-debt-backlog.md. Read that file and CLAUDE.md fully before
writing any code.

PROJECT RULES YOU MUST FOLLOW (violations = incomplete work):
- Python via .venv only: run tests with `.venv\Scripts\python.exe -m pytest -q` from the
  repo root D:\Claude\QuickJoiner (never system python; run from repo root or pytest
  collects nothing).
- New tables/methods go in the backend-neutral `_SqlCatalog` in
  quickjoiner/memory/catalog.py: `?` placeholders only, `ON CONFLICT ... excluded`
  upserts, no engine-specific SQL. Add the table to `_SCHEMA_STATEMENTS` (both backends
  create it automatically — do NOT touch pg_catalog.py). Add every new public method to
  the `CatalogBackend` Protocol in quickjoiner/memory/base.py.
- The SQLite adapter serializes with a lock inside `_write/_read_*` primitives — use those
  primitives, never raw self._conn.
- Capture hooks in agent/tools.py must be fire-and-forget: wrap the gap write in
  try/except Exception and NEVER let it alter the tool's return value.
- Config: add `GapsConfig` pydantic model in quickjoiner/config.py and `gaps` field on
  `Config`. Config persists via catalog settings blob automatically — no other wiring.
- Tests use FakeEmbedder from tests/conftest.py (deterministic whitespace-token hash
  vectors) — craft cluster-test queries with overlapping literal tokens so cosine
  behavior is predictable (e.g. "octopus deploy failed" vs "octopus deploy timeout").
- Frontend: wizard state machine is pure functions in frontend/src/wizard.ts — extend
  startWizard(types, preselectType?) so a valid preselect skips step "type"; keep it
  pure. UI panels follow existing token classes (bg-panel, text-gold, etc. — tokens are
  raw CSS vars; NEVER use Tailwind alpha modifiers like bg-panel/50 on them). New API
  calls go in frontend/src/api.ts following the existing `req<T>` pattern.
- Frontend build: `$env:Path = "$env:LOCALAPPDATA\nvm\v22.23.1;$env:Path"` then
  `npm run build` inside frontend/ (tsc runs first; it must pass).

IMPLEMENTATION ORDER:
1. catalog.py: gaps schema + methods log_gap / list_gaps(status) / resolve_gaps(ids,
   resolution) + protocol entries.
2. config.py: GapsConfig.
3. quickjoiner/gaps.py: cluster_gaps + suggest + TERM_HINTS exactly as the plan's §1
   describes; module-level pure functions.
4. agent/tools.py: capture in search_memory NO_RESULTS branch (near-misses via a second
   store.search with min_score=0.0, top_k=3) + new list_gaps AgentTool registered in
   build_builtin_tools.
5. api/app.py: GET /api/gaps, POST /api/gaps/resolve (pydantic request model near the
   others; auth via _require_user(_user(authorization)) exactly like /api/learn).
6. Frontend: api.ts methods, wizard preselect, Rail badge + GapsPanel component, CTA
   wiring in App.tsx through the existing CommandCtx (do NOT bypass the commands.ts
   pipeline).
7. Tests per the plan's §3 matrix, INCLUDING adding "gaps" to _TABLES in
   tests/test_pg_backend.py and a pg round-trip test.
8. Docs: update CLAUDE.md (architecture bullet for gaps.py + api endpoints; mark PRD W2.1
   partial/complete status note in docs/PRD.md).

DEFINITION OF DONE (verify each, report honestly):
- Full suite green: `.venv\Scripts\python.exe -m pytest -q` (expect prior count + new
  tests, zero regressions).
- Postgres parity: start Docker Desktop, run
  `docker run -d --rm --name qj-test-pg -e POSTGRES_PASSWORD=qj -e POSTGRES_DB=qj -p 55432:5432 pgvector/pgvector:pg16`,
  wait for pg_isready, then run the pg suite with
  QJ_TEST_DATABASE_URL=postgresql://postgres:qj@localhost:55432/qj — all green. Stop the
  container and shut Docker Desktop down afterwards (slow machine).
- `npm run build` clean.
- Behavioral smoke: in a scratch workspace (set QJ_WORKSPACE to a temp dir — NEVER the
  live ~/.quickjoiner/default), ask an unanswerable question via the API with a scripted
  provider or the agent tool directly; confirm GET /api/gaps shows the cluster.
- Every AC in the plan's §2 has at least one asserting test. List AC→test mapping in your
  final report.
```
