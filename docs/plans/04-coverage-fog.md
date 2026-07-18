# Plan 04 — Coverage Fog & One-Click Remediation (PRD W10.1 + W2.3, first Orrery slice)

> **STATUS: ⬜ NOT STARTED.** Its data dependency — the knowledge-debt gaps backlog (`gaps.py`,
> `/api/gaps`, `cluster_gaps`; formerly Plan 01, now shipped and documented in the CLAUDE.md gaps
> bullet) — is ✅ ready. Nothing built yet: no `coverage.py` / `/api/coverage`, no
> `frontend/src/fog.ts`, no GraphView fog layer, and **Phase 0's frontend test harness
> (Vitest/RTL/MSW) does not exist** — the repo still has zero FE unit tests. See [STATUS.md](STATUS.md).

Effort: ~4–5 dev-days (incl. FE test harness) · Dependencies: **gaps backlog (shipped —
`gaps.py` / `/api/gaps` / `cluster_gaps`, see the CLAUDE.md gaps bullet)**
Design source: `docs/design/DESIGN_VISION.md` + `orrery-prototype.html` (port its visual
grammar: dashed frontier, UNCHARTED label, violet heat, "territory lit" instrument).

## 0. Precondition — the frontend test harness (non-negotiable)

TEST_STRATEGY.md is explicit: FE has **zero tests**, and this feature is canvas-adjacent
(the pan-crash lesson). Phase 0 of this plan stands up the harness and banks the cheap
wins so the fog work lands on a net:
- Vitest + React Testing Library + MSW 2 (`frontend/vitest.config.ts`, `src/test/setup.ts`,
  `npm run test` script). jsdom environment; coverage via v8 (advisory this plan, gates per
  TEST_STRATEGY T2).
- First suites (pure logic, fast): `wizard.test.ts` (every step + cancel + preselect),
  `commands.test.ts` (registry precedence, /learn happy+error via MSW, scrape follow-up
  flow consume/dismiss), `api.test.ts` (SSE frame splitting, error extraction),
  `markdown.test.tsx` (citation numbering, mermaid fence flag, the recursion regression).

## 1. Backend — `GET /api/coverage`

New module `quickjoiner/coverage.py` (pure assembly over existing data; no new tables):

```
build_coverage(catalog, config) -> {
  territory_lit_pct: int,          # heuristic, see below
  sources: [{name, type, docs, last_sync, stale: bool}],   # stale = older than 7d w/ interval
  zones: [                          # the dark map
    {kind: "gap-cluster", label, count, suggested_connectors[], entity_hints[], gap_ids[]},
    {kind: "silent-source", label, source_name, docs: 0},   # configured but never synced
    {kind: "missing-category", label, category, suggested_connectors[]},
  ]}
```
- gap-cluster zones come straight from Plan 01's `gaps.cluster_gaps`.
- missing-category: a small static table of persona-critical categories (chat, incident,
  CI) → if no configured source of those types exists AND gaps mention related terms,
  emit a zone ("No chat system connected — 9 refusals mention Slack/Teams terms").
- `territory_lit_pct`: bounded heuristic — `round(100 * synced_sources/(configured+missing_categories_hit) ) `
  minus a capped gap penalty; clamp [5, 98] so it never lies with 0/100. Document the
  formula in the module docstring (it will be tuned; make it one function).
- Endpoint in `api/app.py`, no auth beyond the standard open/user rule (read-only).

## 2. Frontend — fog layer on GraphView + remediation sheet

- **Data adapter first** (`frontend/src/fog.ts`, pure & unit-tested): given the graph
  snapshot's laid-out node positions + `/api/coverage` zones → placement list:
  `{x, y, r, zone}` for each dark zone. Placement: deterministic golden-angle positions
  around the periphery of the occupied bounding box, radius scaled by `count`, no overlap
  with node clusters (min-distance from any node, push outward until clear). Pure function
  of (nodes, zones, viewport) → trivially testable.
- **Rendering** (extend `GraphView.tsx`, SVG is sufficient per FRONTEND_ROADMAP F2 slice
  1): per zone — radial darkening (SVG radialGradient overlay), dashed violet frontier
  circle, `◇ UNCHARTED — <LABEL>` mono caption, violet count badge. Toolbar gains the
  `territory lit %` instrument next to the entities/relationships stamp. Legend gains the
  dashed "uncharted" entry (the Orrery legend already defines it).
- **Interaction**: click zone → `RemediationSheet` (same panel pattern as `NodePanel`):
  zone label, sample refused questions (respecting hash-only privacy mode: show "3
  similar questions" without text), CTA chips:
  - **Connect <type>** → `onRemediate({kind:"connect", type})` → App switches to chat
    view and starts the `/connect` wizard preselected (Plan 01 added
    `startWizard(types, preselectType)`); goes through `CommandCtx`, not around it.
  - **Teach the answer** → chat view, composer prefilled `/learn ` + focus.
  - **Dismiss** → `POST /api/gaps/resolve {resolution:"dismissed"}` → refetch coverage.
- **A11y/reduced-motion**: zones get `role="button"` list fallback — a "Dark zones" list
  under the legend on small screens/list mode; no animation beyond the existing frontier
  dash (reduced-motion: static).

## 3. Acceptance criteria
1. Coverage payload derives ONLY from real data (sources table + gaps) — a fresh
   workspace with one synced source and zero gaps shows zero gap zones and no fake fog.
2. Three refusals about deploys (Plan 01 fixtures) ⇒ one gap-cluster zone whose sheet
   suggests octopus/jira; Connect CTA lands in the wizard with that type chosen.
3. Hash-only privacy mode: sheet shows counts, never query text.
4. Zone placement is deterministic (same inputs → same positions) and never covers a
   node (min-distance property test over random layouts).
5. Dismissing a zone's gaps removes the zone after refetch; territory-lit % rises.
6. Fog layer failure (coverage endpoint 500) degrades to the plain graph — error pill,
   no blank canvas (error-boundary + query error state test).
7. `npm run test` green with the phase-0 suites; `npm run build` clean.
8. Live browser verification on a seeded scratch workspace (scratch port, NEVER the
   user's 8787 server): screenshot fog + sheet + wizard handoff in the final report.

## 4. Test matrix
BE (`test_coverage.py`): lit% formula bounds/clamps, silent-source zone, missing-category
trigger + non-trigger, gap-cluster passthrough, API contract, privacy mode.
FE unit: `fog.test.ts` placement determinism + no-overlap property (seeded random
layouts); `GraphView` zone render + click → sheet (RTL); RemediationSheet CTA callbacks;
MSW-driven refetch-on-dismiss. Phase-0 suites per §0.
E2E (if Playwright is stood up here, else manual): toggle graph → fog visible → zone →
sheet → Connect → wizard shows preselected type.

---

## 5. Implementation prompt (paste into a fresh Claude Code session)

```
Implement the Coverage Fog & Remediation feature for QuickJoiner exactly per
docs/plans/04-coverage-fog.md. Read it; the gaps backlog is your data source and is
already shipped (quickjoiner/gaps.py, catalog gaps table, GET /api/gaps,
gaps.cluster_gaps — see the CLAUDE.md gaps bullet),
docs/design/DESIGN_VISION.md, CLAUDE.md, and frontend/src/components/GraphView.tsx before
coding. Open docs/design/orrery-prototype.html in a browser once — you are porting its
visual grammar (dashed violet frontier, UNCHARTED caption, territory-lit instrument) onto
the real GraphView.

PHASE 0 FIRST (do not skip): stand up the FE test harness — Vitest + React Testing
Library + MSW 2 in frontend/ (vitest.config.ts with jsdom + v8 coverage, src/test/setup.ts,
"test" npm script) and write the four pure-logic suites listed in the plan §0
(wizard/commands/api-sse/markdown). These must pass before any fog code. Node on this
machine: prepend `$env:Path = "$env:LOCALAPPDATA\nvm\v22.23.1;$env:Path"` before any npm
command; builds/tests run inside frontend/.

HARD RULES:
- All remediation actions flow through the existing pipeline: App's CommandCtx →
  startWizard(types, preselectType) / composer prefill. Never spawn a parallel dispatch
  path.
- setState updaters must NEVER dereference mutable refs (this exact class of bug crashed
  GraphView before — there is a CLAUDE.md note; add a regression test for any new
  pointer handling you write).
- Design tokens are raw CSS vars (var(--unknown) = violet, var(--gold), var(--accent));
  no Tailwind alpha modifiers on token classes; SVG fills may use the vars directly.
- The fog data adapter (frontend/src/fog.ts) is a pure function — position math lives
  there, not in the component, so tests target the adapter.
- Backend module quickjoiner/coverage.py is pure assembly over catalog + gaps — no new
  tables, no schema changes (so no pg parity work; state that explicitly).
- Respect prefers-reduced-motion; provide the dark-zones list fallback for a11y.

BUILD ORDER: phase 0 harness+suites → coverage.py + tests → /api/coverage + contract
tests → fog.ts + tests → GraphView layer + RemediationSheet + component tests → App
wiring (onRemediate) → docs.

LIVE VERIFICATION (required, mirrors how GraphView was verified before):
- Seed a scratch workspace under the session scratchpad (set QJ_WORKSPACE; NEVER touch
  ~/.quickjoiner/default), run `qj serve --port 8899` (8787 belongs to the user's own
  server — do not kill it), create gaps by asking 3 unanswerable deploy questions via the
  API, open Chrome via the browser tools, toggle the Knowledge view, screenshot: fog
  zones visible, click → sheet, Connect → wizard preselected. Fix anything the browser
  reveals, re-verify, then stop the scratch server.

DEFINITION OF DONE:
- `.venv\Scripts\python.exe -m pytest -q` green (backend count + new coverage tests).
- `npm run test` green (phase-0 + fog suites); `npm run build` clean.
- Live-verification screenshots referenced in the final report.
- CLAUDE.md updated (coverage.py bullet, /api/coverage, fog layer + FE test harness
  existence); docs/PRD.md W10.1/W2.3 marked shipped; docs/FRONTEND_ROADMAP.md F0 harness
  + F2 slice-1 checkboxes annotated.
- AC→test mapping for all 8 ACs in plan §3, with honest gaps named if any remain.
```
