# QuickJoiner — Testability Program: to >90% and beyond

Authors: RAG engineering + React engineering. 2026-07-11.
Goal: ≥90% line **and** branch coverage on backend and frontend, enforced in CI, plus the
non-coverage dimensions that actually catch bugs (property, parity, mutation, E2E).

## 1. Current state (measured honestly)

- **Backend**: ~399 tests green (local + a pg parity suite, env-gated, that runs against
  Docker), but coverage is *unmeasured* — no `pytest-cov` gate. Strong areas: connectors' pure
  converters, retrieval, graph, API contracts, sessions, evals (incl. threshold calibration +
  report comparison), alias query expansion. Known thin areas listed in §3.
- **Frontend**: **zero automated tests.** TypeScript + one live browser pass is the net.
- **E2E**: manual browser verification only (it caught the pan crash — proof this layer pays).

## 2. Principles

1. **Coverage is the floor, not the goal**: every % must come from asserting behavior, not
   from executing lines. PR review rejects assert-free tests.
2. **Determinism everywhere**: FakeEmbedder (no downloads), ScriptedProvider (no LLM),
   MockTransport (no network), MSW (no server). CI never touches the internet.
3. **Parity is a test dimension**: SQLite↔Postgres must stay behaviorally identical; the pg
   suite runs in CI via a service container, not only on demand.
4. **The refusal contract gets its own suite**: grounding regressions are release blockers
   independent of coverage numbers.

## 3. Backend plan

### 3.1 Tooling
- `pytest-cov` with `fail_under=90` (line+branch) on `quickjoiner/*`; per-module report in CI.
- `hypothesis` for property tests; `mutmut` monthly on core modules (target ≥60% killed).
- GitHub Actions matrix: `{windows, ubuntu} × {3.11, 3.12}`; ubuntu job adds
  `pgvector/pgvector:pg16` service container and sets `QJ_TEST_DATABASE_URL` so the parity
  suite always runs.

### 3.2 Gap-closing test cases by module (the work list)

| Module | Missing coverage | Test cases to add |
|---|---|---|
| `llm/anthropic_provider.py` | wire format, keyless paths | Golden-file `_to_wire` tests: tool results land in ONE user message; thinking_blocks re-emitted FIRST; stream event → on_stream mapping (fake SDK client); missing-key raises cleanly |
| `llm/ollama_provider.py` | native /api/chat wiring | MockTransport: request payload shape (tools, think flag), streamed chunk parsing incl. split JSON lines, tool-call delta assembly |
| `cli.py` | most commands untested | Typer `CliRunner`: init (idempotent), learn file/text, connect --share/--private, sync unknown source error, ask --no-stream with ScriptedProvider, sessions list/distill, eval --init, export -f md/csv; exit codes |
| `api/app.py` | auth-required branches, settings PATCH edge cases, briefs 404/502, SSE error events | 401 matrix across mutating endpoints with auth enabled; secret masking round-trip (PATCH with MASKED keeps stored secret); chat SSE provider-crash → error event then done; /api/graph limit param |
| `api/hooks.py` | signature edge cases | wrong sig → 401; missing header; replayed body; unknown source → 404; upsert_source-before-ingest invariant |
| `scheduler.py` | untested | fake clock / trigger fire → sync called once per interval; overlapping-run guard; source without interval never scheduled |
| `connectors/browser/session.py` | untested (needs Playwright) | contract tests with a fake `sync_playwright` module: profile dir creation, DESKTOP_UA + stealth JS applied, `has_profile` logic |
| `connectors/*` live tools | tool fn bodies | each `tools()` fn with MockTransport: JQL/WIQL/code-search request shape + result formatting + HTTP error → readable message |
| `export.py` | pptx/csv branches | golden files per format; csv from markdown tables; pptx slide count/titles |
| `memory/embedder.py` | ollama embedder | MockTransport: batch request, dim probe caching |
| `evals/harness.py` | ✅ agent-layer branches, calibration + comparison covered (`test_evals.py`): calibrate clean-separation, monotone curve, plateau-midpoint (not edge), floor-unreachable, thin-set flag, apply round-trip; `compare_reports` regression/tolerance/direction; remaining: report JSON schema snapshot |
| `memory/expansion.py` | ✅ covered (`test_expansion.py`): window lookup incl. 4-token, longest-window-first, stopword-only skip, cap, no-double-append, end-to-end retrieval lift on FakeEmbedder store, `search_memory`/config wiring; remaining: `/api/search` wiring assertion |
| `ingest/pipeline.py` | error accumulation | doc that raises mid-iteration → stats.errors, rest ingested; ensure_ann_index absent on store (PG) is a no-op |
| `auth.py` | token lifecycle edges | logout unknown token; open-mode → first user flips enabled; PBKDF2 verify negative |

### 3.3 Property-based suites (hypothesis)
- `normalize_text`: idempotent; never grows control chars; length-bounded.
- `chunkers`: reassembly covers all non-whitespace content; chunk size bounds hold.
- `rrf_fuse`: permutation-stable ties; monotonic under leg truncation; no dupes.
- `parse_triples` / `ticket_keys`: never crash on arbitrary unicode; outputs always
  vocabulary-valid / stoplist-clean.
- `aliases`: output forms never empty strings; idempotent lowercase.

### 3.4 Contract & integration
- **Store contract suite**: one parametrized test class run against `KnowledgeStore` and
  `PgVectorStore` (upsert/delete/search/hybrid-rescue/gate) — replaces ad-hoc parity tests
  with a single source of truth.
- **Catalog contract suite**: same pattern over `Catalog` / `PostgresCatalog` (config,
  sources, sessions, users, graph).
- **Server-in-process E2E**: httpx against the FastAPI app for the golden path:
  init → connect(files) → sync → ask (scripted provider) → graph → distill → gaps.
- **Crash-safety** (NFR N6): kill ingest mid-batch (exception injection at chunk N) → rerun
  → no dupes, counts correct.

### 3.5 The grounding regression suite (release blocker)
- Frozen mini-corpus + eval pack in `tests/fixtures/eval_smoke/`: recall@5 = 1.0 on 10
  planted facts, refusal on 10 planted absences, with FakeEmbedder; runs in <5 s.
- Real-embedding nightly job (bge-small cached): thresholds from `qj eval` must not regress
  >2 pts vs the committed baseline JSON.

## 4. Frontend plan (0 → 90)

### 4.1 Tooling
Vitest (+ v8 coverage, `thresholds: {lines: 90, branches: 90}` on `src/**` excluding
`main.tsx`), React Testing Library, MSW 2 (REST + streamed SSE responses), Playwright
(chromium; webkit weekly), axe-core in Playwright, Storybook + screenshot regression (F1).

### 4.2 Unit/component matrix

| Target | Cases |
|---|---|
| `wizard.ts` (pure) | full happy path per step; cancel at every step; invalid type re-prompt; required-field re-ask; list field split; secrets hint present; failsave both branches; sync yes/no — *the cheapest 100% in the codebase* |
| `commands.ts` | registry match precedence; /learn success+error (MSW); /scrape event sequence incl. artifact + follow-up flow set; follow-up yes creates connector + syncs, no dismisses, other input falls through; wizard flow consumes all input |
| `api.ts` | SSE parser: split frames across chunks, non-ok JSON error extraction, auth header injection, token storage |
| `markdown.tsx` | citation dedup numbering; bold/italic/code nesting; fence vs mermaid fence (flag on/off); the recursion/lastIndex regression (long bold-in-bold input completes); list/heading/paragraph boundaries |
| `MermaidBlock` | render success (mock mermaid module), parse error → code fallback, unmount during async render (no setState-after-unmount) |
| `Chat.tsx` | refusal badge regex table; ledger ordering; artifact chip callback; agent plain-text branch; streaming caret states |
| `graph/camera.ts` (pure) | `zoomAround` keeps the anchor screen point fixed; `clampScale` honours both the fixed floor and a content-derived `minK`; `approach` is frame-rate independent (same end state for 1×16ms vs 4×4ms) and interpolates scale geometrically; `fitCamera` never zooms past `maxK` and centres the extent; fling `decayVelocity`/`clampFling` monotonic + capped; **`markScale` keeps painted size inside [0.62, 1.85] across the whole zoom range — the "everything becomes tiny" regression** |
| `graph/lod.ts` (pure) | culls to the overscan rect; pinned ids survive both the viewport cull and the budget; budget keeps highest-degree first and sets `budgetLimited`; no edge is emitted whose endpoint was culled; `lodKey` is stable within a tile and changes across tiles |
| `graph/simulation.ts` | `setData` carries over position+velocity for surviving nodes; new nodes seed on an anchor neighbour, not the origin; degree recomputed per data set; `hot()` goes false after settling |
| `GraphView` | type→color map totality; neighbor dimming set; **pan updater never dereferences a nulled ref (regression test via pointer event sequence)**; a press below `NODE_DRAG_SLOP` neither pins nor reheats the layout (the double-click-loses-its-target regression); double-click expands the *pressed* node, not `e.target`; alias search 404 → error pill |
| `ArtifactModal` | learn button state machine idle→busy→done; download blob name; ESC/backdrop close |
| `App` routing | stage order: flow > command > chat (spy on streamChat); busy lockout |

### 4.3 Playwright E2E (against the real server, ScriptedProvider via env flag or MSW-at-edge)
1. Cold start → empty state → `/learn` → counters update → ask → cited answer renders.
2. `/scrape` (mocked SSE) → progress → modal opens → mermaid SVG present → Learn → toast.
3. `/connect` wizard full run with test-failure → save-anyway path.
4. Graph: toggle view → node click → evidence panel → alias search focus → **drag-pan 50
   events → no crash** (the regression, forever).
5. a11y: axe scan on chat, settings, graph, modal — zero serious violations.
6. Theme toggle: both themes screenshot-diffed on chat + graph.

### 4.4 What FE coverage excludes (declared, not hidden)
Generated `dist/`, font assets, `vite-env.d.ts`. Everything else counts.

## 5. CI pipeline (single workflow, ~10 min budget)

```
lint+typecheck (py+ts) ──┐
pytest matrix + cov ─────┼─► coverage gates (BE≥90, FE≥90) ─► build ─► Playwright suite
vitest + cov ────────────┘         │
pg service parity suite ───────────┘        nightly: real-embedding evals · weekly: mutmut, webkit
```

Merge policy: red gate = no merge; grounding suite failure = no release regardless of green
coverage.

## 6. Milestones

| Milestone | Definition of done |
|---|---|
| T1 (2 wks) | Coverage measured + published; FE harness live; wizard/commands/api.ts/markdown at 100%; CI gates advisory |
| T2 (4 wks) | BE ≥90% (gap table §3.2 closed); FE ≥90%; gates enforcing; pg suite in CI |
| T3 (6 wks) | Playwright suite + a11y green; grounding regression suite blocking releases; property suites merged |
| T4 (8 wks) | Mutation baseline ≥60% on retrieval/graph/pipeline; store+catalog contract suites replace ad-hoc parity |
