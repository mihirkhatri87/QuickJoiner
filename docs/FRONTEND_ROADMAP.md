# QuickJoiner — Frontend Architecture Roadmap

Author: React engineering, with UX. 2026-07-11.
Inputs: `PRD.md` (W1/W2/W10 especially), `design/DESIGN_VISION.md`, `TEST_STRATEGY.md`.

## 0. Where we are (honest inventory)

Shipped: Vite + React 18 + TS + Tailwind; "evidence ledger" design system in CSS tokens;
hand-rolled markdown renderer (citations→ledger, mermaid lazy-loaded); SSE client; command
pipeline (`commands.ts`: Flow interceptors → Command registry → agent chat); `/connect`
wizard state machine; d3-force GraphView with evidence panel; artifact modal.

Debt, named:
1. **Zero automated FE tests** (the pan-crash bug proved the cost).
2. All server state is hand-rolled `useState` + fetch in `App.tsx` (~450 lines) — no cache,
   no retries, no invalidation discipline.
3. No routing: views are booleans; graph entities/dossiers aren't linkable.
4. Command turns (wizard, scrape, learn) aren't persisted to sessions — refresh loses them.
5. Hand-rolled markdown is fine today but will strain (tables, nested lists).
6. Graph is SVG — comfortable to ~1–2k nodes, not 20k.
7. No error boundaries, no web-vitals telemetry, no a11y audit, no Storybook.

## Phase F0 — Foundations (with R2 "Prove it")

*Goal: never ship another UI change without a net.*

- **Testing stack**: Vitest + React Testing Library + MSW (API mocking incl. SSE), Playwright
  E2E. Coverage gate ≥90% per `TEST_STRATEGY.md` §FE. First targets are the pure logic that
  already exists and tests trivially: `wizard.ts`, `commands.ts` flows, SSE parser,
  `markdown.tsx` (citation dedupe, mermaid fences, the local-regex recursion gotcha).
- **Server state → TanStack Query**: status/sources/projects/sessions/connectors/graph as
  queries with invalidation on mutations (learn/sync/connector CRUD). Kills a whole class of
  "stale counters" bugs; retries and loading states for free. SSE stays bespoke (it's good).
- **Routing → TanStack Router**: `/chat/:sessionId?`, `/graph/:entityId?`, `/settings`.
  Deep-linkable graph entities are a product feature (share "look at this dependency").
- **Error boundaries** per surface (chat, graph, modal) with a diagnostic panel — a crash in
  the canvas must never blank the app again (learned that live).
- **CI**: typecheck + lint (eslint@ts, prettier) + vitest + Playwright smoke on PR; build
  artifact uploaded.
- **GraphView density budget**: `/api/graph?limit=400` divides its budget evenly across
  distinct source entities, so a big graph renders as stubs — on the live AppRiver workspace
  (1,571 source entities) that is a **5-edge cap per node, ~2.8% of 14,172 edges**, which
  reads as "nothing is connected" on a graph that is in fact dense. Make the budget adaptive
  (raise the default; scale the per-entity cap with viewport/zoom), say what was elided
  ("showing 400 of 14,172 edges") rather than silently truncating, and lead with focus:
  entity search returns a full neighbourhood, so make that the primary entry to the view.

## Phase F1 — Product hardening (with R3 "Team product")

- **Persist command turns** (PRD gap): lightweight `POST /api/sessions/:id/events` so
  wizard/scrape/learn exchanges replay after refresh; App state hydrates from it.
- **Design system extraction**: tokens → typed theme module; primitives (`ui.tsx`) →
  documented components with Storybook + visual regression (Chromatic or Playwright
  screenshots). The Orrery work will fork these tokens; they must be stable first.
- **Command palette (cmdk-style)**: keyboard-first access to commands, sessions, entities,
  connectors; the `/` commands become discoverable. The registry from `commands.ts` is
  already the right shape to feed it — one provider, two surfaces.
- **Markdown**: keep the hand-rolled renderer as the fast path; add conformance tests; adopt
  a parser (`marked` + custom renderer) only when tables/footnotes demand it — with a
  side-by-side golden-file suite before swapping.
- **Analytics dashboard (W1)** and **gap backlog (W2)** views: new routes, chart primitives
  (sparkline/area/bar) built on the design tokens — no chart mega-library; follow dataviz
  discipline (tabular-nums, endpoint emphasis, both themes).
- **Accessibility pass to WCAG 2.2 AA**: axe in CI, keyboard paths for wizard/modal/graph
  (graph needs a list-mode fallback — the a11y answer to canvas), focus management in modal.
- **Perf budgets**: initial JS ≤ 250 KB gz (mermaid/d3/graph stay lazy); route-level code
  splitting; web-vitals beacon to the (self-hosted) telemetry endpoint.

## Phase F2 — The Orrery, incrementally (with R4 "Category maker")

Design fiction → features, in shippable slices (see `design/DESIGN_VISION.md` §path):

1. **Fog layer on GraphView** (W10.1): coverage + refusal-heat overlay computed from
   `/api/graph` + `/api/gaps`; dark-zone click → remediation sheet (wired to /connect
   prefill). SVG is fine for this slice.
2. **Canvas upgrade**: swap SVG for WebGL rendering (sigma.js or regl custom) behind the
   same data adapter once node counts or the fog shader demand it; keep the SVG renderer as
   the reduced-motion/a11y fallback. Layout moves to a worker (force simulation off the
   main thread).
3. **Intent line + dossiers**: map mode where the composer docks over the canvas; answers
   render as pinned dossier cards with evidence-thread highlighting (citation → chunk →
   doc → node joins already exist server-side).
4. **Ramp ribbon** fed by W1 analytics; territory diff ("what lit up this week") from sync
   deltas (W6.2).
5. **Ambient/desktop**: PWA (offline read of briefs/dossiers); evaluate a Tauri shell for
   the air-gapped tier (native fs, tray, no Chrome dependency) — decision gate, not a
   commitment.

## Architectural rules going forward

1. **The pipeline stays the spine**: every new input surface (palette, intent line, Slack
   deep links) feeds the same Flow → Command → Agent chain. No parallel dispatchers.
2. **Server truth via queries; ephemeral UI via local state; long-lived flows via explicit
   machines** (the wizard pattern). No global state library until a third category appears.
3. **Canvas never owns data**: renderers (SVG/WebGL/list) consume one graph adapter, so the
   a11y fallback and tests target the adapter, not the pixels.
4. **setState updaters never touch refs** (codified from the pan-crash); lint rule added.
5. **Every visual token has a name**: raw hex in a component is a review blocker.

## Sequencing summary

| Phase | Exit criteria |
|---|---|
| F0 | FE coverage ≥90% on logic modules; CI gates on; router + query layer merged; zero `App.tsx` fetch calls |
| F1 | Command turns persist; Storybook live; axe clean; palette shipped; W1/W2 views on real APIs |
| F2 | Fog layer GA; dossier mode behind flag; WebGL renderer decision made with benchmarks |
