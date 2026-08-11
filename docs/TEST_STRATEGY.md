# QuickJoiner — Testability Program: to >90% and beyond

Authors: RAG engineering + React engineering. 2026-07-11.
Goal: ≥90% line **and** branch coverage on backend and frontend, enforced in CI, plus the
non-coverage dimensions that actually catch bugs (property, parity, mutation, E2E).

## 1. Current state (measured honestly)

- **Backend**: **1029 tests green** (2026-08-10; local + a pg parity suite, env-gated, that runs
  against Docker), but coverage is *unmeasured* — no `pytest-cov` gate. Strong areas: connectors'
  pure converters, retrieval, graph, API contracts, sessions, evals (incl. threshold calibration +
  report comparison), alias query expansion, skills (written as leak tests), and relative-date
  resolution (`test_dates.py` — these pin the *conventions*, not just the parsing, because the
  failure they exist to stop is a plausible-looking window off by a day or a week that returns
  real rows from the wrong period). Known thin areas listed in §3.
- **Standing lesson, re-earned 2026-08-10**: a green suite proves a feature does what it says,
  never that what it says is worth saying. Two HIGH defects in the skills feature — a skill whose
  frontmatter name differed from its folder was undeletable from *every* layer, and
  `requires_env: []` could not declare "needs nothing" — sat under a fully green 952-test suite,
  because every test drove the zip-install path where name and folder always agree. Both were
  found by an adversarial read of the module, not by running it.
- **Flake watch**: `test_sync_then_sources_and_search` was failing ~1 run in 4 (2026-07-30). Not a
  product bug — contextual chunking puts the document's **uri** in the chunk breadcrumb, and under
  `FakeEmbedder`'s 32-bucket hash the random pytest tmp path moves the cosine ±0.06, straddling the
  new `min_score=0.64` gate (at the old 0.55 it never crossed). Fixed the same way two sibling tests
  already were: bypass `min_score` when the assertion is about *memory being searchable*, not about
  grounding behaviour. **Standing lesson: any test asserting a hit under the real threshold with
  FakeEmbedder is a coin flip on the temp directory's name** — assert grounding behaviour only where
  the score is genuinely the subject, with fixed text.
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
| `connectors/onedrive.py` + `msgraph.py` | ✅ covered (`test_onedrive.py`, 34): sharing-token encoding round-trip, least-privilege scope composition, `skip_reason` per document type incl. **image = "not yet" ≠ unsupported**, webUrl-as-uri (+ stable fallback), `remoteItem` following, manifest dedupe, search/notification shapes; PKCE = S256(verifier), refresh-token-preserved-when-omitted, expiry skew, device-code `authorization_pending`/`slow_down`, `invalid_grant` → GraphAuthError, MockTransport refresh **persists the rotated token**; on-demand learn (shared link → download → manifest), folder expansion, skips reported not swallowed, sync refreshes only learned items and **never enumerates**, webhook can't widen ingest, `on_deleted` removes the token, tool consolidation + selector. Plus 6 HTTP tests in `test_api.py` (create-before-sign-in, status leaks no token, PKCE authorize URL, forged-state callback, learn guards, delete removes the token). **Remaining: no live tenant** — no Microsoft 365 app registration on this machine, so Graph payload shapes are pinned against the documented contracts via MockTransport, not observed. A one-off live smoke (sign in, learn one real document) is the honest gap. |
| `chat_attachments.py` promote-to-memory | ✅ covered (`test_chat_attachments.py`): learn ingests into the Uploads source and the document is really there, the attachment stays downloadable afterwards, unknown id → 404 vs swept bytes → **410** (re-attach, don't hunt for a typo), and the context block carries each file's id + the qj_api learn path while keeping the "NOT long-term memory" default |
| `memory` scoping + labels | ✅ covered (`test_scoping.py`, 17): `label_applies` across connector/folder/document; a **folder tag covering documents ingested after it was set** (the whole point of prefix rules); a connector-wide tag resolving to a source_id rather than enumerating documents; `%`/`_` in a real path not widening the LIKE; scoped search returning only in-scope hits when identical text exists in two sources; scoping by doc_id across both hybrid legs; **empty scope byte-identical to no scope**; a scope matching nothing returning nothing rather than silently widening; a connector name containing a quote not breaking the LanceDB predicate; a scoped turn withholding live tools for excluded connectors; and a scoped refusal telling the model it only searched a slice. **pgvector parity for the scope predicate now covered** (2026-08-04, `test_pg_backend.py::test_pg_knowledge_scopes_filter_identically_to_sqlite`, executed against a real pgvector container rather than statically reviewed) |
| Knowledge scopes (per-user visibility) | ✅ covered (`test_knowledge_scopes.py`, 28) — written as **leak tests**, since this is a security property and a test that only proves the happy path proves nothing. Pure layer: visibility AND-ed over picks (not OR-ed, which would let naming a source escalate into reading it); `[]` matching no row where the naive `IN ()` would have failed the filter *open*; `None` byte-identical to the old unscoped path. Catalog: buckets covered as well as connectors; sharing returns a source to the commons. Retrieval: private source unreachable; escalation refused via both `source_ids` and `doc_ids`; permitted hits keep their exact scores (the gate must not move when auth is switched on); quoted username can't break the predicate. Graph: neighbours, the `graph_relations` enumeration, corroboration counts, path routing, expansion, entity autocomplete and totals each filtered — plus the deliberate split that an evidence-free `same_as` bridge survives while a dangling edge fails closed. Ownership: private-by-default note buckets, open-mode commons preserved, `share` reaching everyone. End-to-end: a two-user API test **verified to fail with the filter disabled**. **Write half added 2026-08-10** (Cloud Y1.8 (c)+(f), now complete): four merge-guard leak tests — a private document cannot merge its wording into an org entity, rename one, alias one, or bridge one — each **verified to fail with the guard disabled**, against a *merge-everything* adjudicator so the guard is what stops it rather than a cautious model; three controls that must pass either way (the same document from a shared source still merges, attaching to an org entity still works, open-mode ingestion is untouched), which is what proves the guard bites on privacy alone and not on ingestion generally. Promotion: approval publishes to another user and keeps the same doc_id; a decline changes nothing; **an offer alone publishes nothing** (the back door the review exists to prevent); only the owner may offer; already-public and never-offered are refused rather than silent no-ops; entity release is scoped to the promoted document; the `personal-note` class scores below the same claim from the commons and the discount **lifts on promotion**. API: the queue is a reviewer capability (a viewer gets 403), the reviewer-only read exception 404s for anything not offered, and search goes from empty to a hit across the decision. Postgres parity for the insert-only upsert, the bridge query, entity rehoming and the in-place source move is **executed against a real pgvector container** |
| `ingest/extract.py` damaged packages | ✅ covered (`test_extract.py`): a deck with one CRC-corrupt media part — `Presentation()` provably raises — still yields every slide's text with slide structure intact; the docx salvage reads body + header parts; a file that is not an Office package at all still raises ExtractionError (salvage must not turn 'not a deck' into silence) |
| `ingest/extract.py` Office shape trees | ✅ covered (`test_extract.py`): **shapes python-pptx cannot see at all** — an `mc:AlternateContent`-wrapped shape is extracted even though `len(slide.shapes)` proves the object model misses it, without double-counting its Choice/Fallback copies, and the raw sweep formats tables identically to the structured pass so the two dedupe; plus PowerPoint labels inside **grouped** shapes and nested groups are extracted (the user-reported total-loss bug — an architecture deck is a group of labelled boxes), no duplication of text already captured, and Word text boxes (`w:txbxContent`) + headers/footers that `document.paragraphs` never reaches. **Remaining: SmartArt and embedded charts** are implemented (diagramData relationship / chart part) but not unit-tested — building those fixtures by hand is fiddly; they degrade to "nothing extracted" rather than an error if wrong |
| `ingest/extract.py` archives/images | ✅ covered (`test_extract.py`): zip expands members through `extract_text` with per-member provenance headers, nested archive listed-not-opened (bomb guard), skips reported in an archive-notes block, corrupt zip → ExtractionError, image raises a **vision-specific** error and works once an `ImageHandler` is supplied, widened text/code extension set |
| `export.py` | pptx/csv branches | golden files per format; csv from markdown tables; pptx slide count/titles |
| `memory/embedder.py` | ollama embedder | MockTransport: batch request, dim probe caching |
| `evals/harness.py` | ✅ agent-layer branches, calibration + comparison covered (`test_evals.py`): calibrate clean-separation, monotone curve, plateau-midpoint (not edge), floor-unreachable, thin-set flag, apply round-trip; `compare_reports` regression/tolerance/direction; remaining: report JSON schema snapshot |
| `memory/expansion.py` | ✅ covered (`test_expansion.py`): window lookup incl. 4-token, longest-window-first, stopword-only skip, cap, no-double-append, end-to-end retrieval lift on FakeEmbedder store, `search_memory`/config wiring; remaining: `/api/search` wiring assertion |
| `ingest/pipeline.py` | error accumulation | doc that raises mid-iteration → stats.errors, rest ingested; ensure_ann_index absent on store (PG) is a no-op |
| `ingest/pipeline.py` graph-pending drain | ✅ covered (`test_triples.py`, 2026-07-30): a document stranded by a **stopped** run (the real shape of the bug, produced with a cancelling `SyncControl` rather than by mocking) is shown to have no edges at all, then rebuilt from its stored chunks — including the connector-supplied deterministic payload — with the extractor asserted to receive the *document's* text, not the breadcrumb-prefixed chunk. The three honesty properties each have their own test: a row predating `graph_pending.graph_json` counts as `text_only` and its unrecoverable connector edge is **not invented**; a document whose chunks are gone stays queued rather than being resolved empty; a scoped drain leaves other sources alone while still sweeping orphan rows. Plus job-level behaviour in `test_sync_manager.py` (refuses with no extractor, mutual exclusion with syncs both ways, stop leaves the rest queued) and the endpoints in `test_api.py`. **Remaining: no Postgres run** of `list_graph_pending`/`sweep_orphan_graph_pending` — portable `?`-SQL in the neutral base, SQLite-verified only |
| `ingest/triples.py` relation signatures | ✅ covered (`test_sessions.py`, 2026-07-30): the motivating impossible line (`environment: prod \| owns \| person: bob`) is dropped while the same relation the right way round survives, across four shapes (inverted publisher/topic, place-vs-container `part_of`, non-software `deploys`). Two lockstep guards do the durable work: every relation in `TRIPLE_RELS` must declare a signature or be listed deliberately unsigned, and every shape the **deterministic** extractors already emit must satisfy the table — so a future signature can't silently start deleting real connector edges |
| `memory/store.py` ANN scoring | ✅ covered (`test_retrieval.py`, 2026-07-29): the suite now **builds a real IVF_PQ index** (400 rows over `ann_min_rows=200` — LanceDB needs ≥256 to train PQ) and asserts the indexed score is within 0.03 of exact brute force, closing the hole that let the product-quantization defect ship: every prior test either stubbed `ensure_ann_index` to assert it was *called* or ran corpora far below the 4000-row default, so **no test had ever built an index**. Verified to genuinely fail (0.28 off) with `refine_factor` reverted. Plus `ann_refine_factor=0` — the obvious way to "turn it off" — is asserted to be **rejected at config validation**, because LanceDB raises on it and it would otherwise break every dense search on an indexed workspace |
| `catalog.upsert_entity` name preference | ✅ covered (`test_graph.py` SQLite + `test_pg_backend.py` Postgres): an LLM-proposed lowercase name cannot clobber a well-cased incumbent, an all-lowercase incumbent *is* upgraded, and a genuinely different name still applies as a rename. Resolved inside the `ON CONFLICT` statement rather than read-then-write, so it costs no extra round trip on the hottest graph-write path and cannot lose a race between concurrent source syncs. **Remaining: the Postgres twin is env-gated and was not executed** (no Docker on this machine) — the shared statement is parsed by both engines on every insert, so a syntax error would surface, but the CASE *semantics* are SQLite-verified only |
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
- **Speed/cost regressions have the same shape of gate** (2026-07-31): `qj bench --compare`
  exits non-zero when a watched metric gets >20% slower or more expensive. Relative, not
  absolute — +5ms means nothing at 500ms and everything at 8ms — and a metric with fewer
  than 5 samples is reported but never gated, so the gate cannot fire on noise. The harness
  itself is unit-tested (`tests/test_bench.py`) with no wall-clock assertions anywhere: a
  test that fails when the machine is busy teaches everyone to ignore it.

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
