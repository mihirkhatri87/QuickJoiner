# Plan 06 — Multi-Angle, Confidence-Scored Answers + Clarifying Questions

Effort: ~7–9 dev-days · Dependencies: none to start section A; C benefits from 01 (gaps) and
the repo-graph-refresh follow-up · Gate: the worked example below (Connector/Nautical/
Stevedore) must produce the *right* answer, not just *an* answer, with a covering test.

## 0. The motivating case (2026-07-15 session)

Asked "how are nautical and connector connected?" the agent called `graph_path`, which
returns the **shortest recorded chain**. Two real, evidence-backed edges existed:

- `service:connector --depends_on--> service:appriver.nautical` (1 hop, evidence: a
  Confluence page titled "Jan 6, 2026" — informal meeting notes, agenda item "Usage from
  connector to Nautical", body text: *"Usage to Nautical from Connector... too many events.
  Noise. **with ASB based approach**"*)
- `service:connector --depends_on--> service:stevedore <--depends_on-- service:appriver.nautical`
  (3 hops, evidence: `Connector/AGENTS.md` + `Connector/README.md` + the dedicated
  "AppRiver.Nautical" Confluence page — the actual documented mechanism: Nautical emits
  usage events onto Azure ServiceBus, Stevedore consumes them and forwards to Connector)

Shortest-path always wins the 1-hop edge, regardless of which chain is more informative or
better evidenced. The user knew the real (longer) mechanism personally and immediately
caught the wrong answer. Two follow-on findings from the same session, folded into this
plan as related work (§5):

- The user has not yet connected Nautical or Stevedore as their own sources — QuickJoiner
  currently only knows about them through what *other* systems (Connector's docs,
  Confluence) say about them. Direct ingestion would very likely produce more specific,
  higher-confidence edges for exactly this scenario.
- Meeting-notes-style Confluence pages are a real vector for the LLM triple extractor
  (`ingest/triples.py`) to collapse a loose, multi-topic discussion into an
  over-confident, coarse `depends_on` edge that has no way to beat a deliberate
  architecture doc's edge except by winning on hop-count.

## 1. Design

### A. Ambiguity detection + one clarifying question (ships first)

The cheapest, highest-value slice: when a relationship question has more than one
materially different recorded answer, don't silently pick — ask.

- `catalog.graph_path` (`memory/catalog.py`) grows a sibling,
  `graph_path_candidates(src_id, dst_id, max_hops, max_candidates=3) -> list[list[dict]]`:
  BFS as today for the shortest chain, then continue exploring (bounded by
  `max_candidates`) for chains that differ either in hop-count *or* in which relation
  types/evidence-source-types they pass through, so a 1-hop `depends_on` from a note and a
  3-hop chain through `AGENTS.md` both surface instead of only the shorter one silently
  winning. Existing `graph_path` (single chain) stays as a thin wrapper for backward
  compatibility (agent tool, `/api/graph/path`, the web UI's Path Finder all keep working
  unchanged) — nothing currently calling it regresses.
- `agent/tools.py::graph_path` tool: when `graph_path_candidates` returns more than one
  materially different chain, the tool's text output says so explicitly — *"Two distinct
  recorded connections exist between X and Y: (1) a direct one-hop claim from
  <source-kind>, (2) a 3-hop chain through <intermediate> from <source-kind>. State both
  with evidence, or ask which the user meant, rather than presenting only one as *the*
  answer."* This is a prompt-facing instruction inside the tool's own return text (same
  pattern the tool already uses for hub-entity capping — see `_NEIGHBORS_FULL_LIST_MAX` in
  the same file), not a new mechanism.
- `agent/prompts.py`: add explicit guidance — *"If your tools surface more than one
  materially different candidate relationship, and the question doesn't already
  disambiguate (e.g. 'architecturally' vs 'organizationally'), ask ONE short clarifying
  question naming the specific interpretations before committing to an answer. Don't guess
  silently when the graph itself recorded more than one distinct claim."* A clarifying
  question is just a normal assistant turn with no tool calls — `agent/agent.py`'s loop
  already returns `result.text` whether it's a real answer or a question; no loop changes
  needed.

### B. Confidence scoring (deterministic, not LLM self-report)

Every existing invariant in this codebase computes grounding from real signals (dense
cosine, hop count, source presence) rather than trusting the model's self-assessment —
confidence scoring follows the same principle.

- New pure module `quickjoiner/agent/confidence.py`:
  `score_edge(evidence_kind: str, source_id: str, corroboration_count: int) -> float`.
  Weights, in order of what this session actually demonstrated mattering:
  - Evidence document **kind/shape**: a dedicated architecture doc (`AGENTS.md`, a
    README, a service-description wiki page) scores higher than a meeting-notes/journal
    page. Detect the latter heuristically (title matches a date pattern, or the page's own
    `kind`/title looks like "Meeting notes"/"Retrospective" — reuse the existing date-title
    patterns already visible in the Confluence corpus, e.g. `\d{4}-\d{2}-\d{2}` or
    "Month D, YYYY").
  - **Corroboration**: how many independent documents (and how many independent
    *sources* — code vs wiki counts more than two wiki pages) assert the same edge.
    `replace_doc_edges` already tracks `evidence_doc_id` per edge; count distinct
    `(evidence_doc_id)` per `(src, rel, dst)` tuple as a query, not a new column.
  - Hop count is *not* a confidence signal on its own (this session's whole lesson) — it's
    only used to decide when two chains are "materially different" for §A's ambiguity check.
- `graph_path_candidates` and `graph_neighbors`'s tool-facing text both annotate each
  edge/chain with its computed confidence, so the model states it (*"lower-confidence:
  sourced only from informal meeting notes"*) rather than presenting every recorded claim
  as equally solid.

### C. Multi-source candidate answers + a traversal UI

For the (less common) case where the user explicitly wants to see multiple flavors rather
than get one clarifying question:

- Structured output convention, parsed the same way `sessions.py::parse_triples` already
  parses a RELATIONSHIPS block strictly (off-format lines dropped, never invented, capped
  count) — a fenced ` ```candidates ` block:
  `<rank>. <one-line summary> | confidence=<0.00-1.00> | sources: <comma-separated tags>`.
  New `quickjoiner/agent/candidates.py`: `parse_candidates(text) -> list[Candidate]`,
  mirroring `triples.py`'s validation strictness (malformed lines dropped silently, cap at
  5).
- `agent/agent.py`: after the model's final turn, if its text contains a `candidates`
  block, split it into `(prose_prefix, candidates)` and emit both; `on_event("candidates",
  json.dumps([...]))` alongside the existing `"answer"` event. No change to the tool-call
  loop itself — this is a post-processing step on the final assistant turn only.
- `api/app.py`'s chat SSE worker: forward the new event type unchanged (it already forwards
  whatever `on_event` emits — see the `lambda etype, detail: events.put(...)` at the
  `ctx.build_agent(...).ask(...)` call site).
- Frontend (`frontend/src/App.tsx` SSE switch, `~line 202`, and a new
  `frontend/src/components/CandidateCarousel.tsx`): when a `candidates` event arrives,
  render a compact ranked strip below the prose answer — confidence badge, a one-line
  summary per candidate, prev/next (or tab) controls, and clicking one expands its
  supporting evidence citations using the exact same citation-chip machinery `Chat.tsx`
  already has (`CiteBook`/`renderMarkdown` from `components/markdown.tsx` — reuse, don't
  reinvent).
- Grounding contract stays untouched: every candidate still must cite real evidence
  documents; `parse_candidates` drops any candidate whose `sources` don't resolve to real
  citations already present in the turn, the same way `parse_triples` drops off-vocabulary
  triples rather than repairing them.

## 2. Acceptance criteria

1. The worked example: asking "how are nautical and connector connected?" against the real
   workspace either (a) asks a clarifying question distinguishing the two chains, or (b) if
   asked to just answer, states the Stevedore/ServiceBus chain and explicitly flags the
   informal meeting-notes edge as lower-confidence — it must never again present the
   `AppRiver.Connector.Monitor`/"Development" tangent as *the* answer with no caveat.
2. `graph_path_candidates` returns the existing single-chain `graph_path` result unchanged
   when only one materially distinct chain exists (no regressions on any current
   `graph_path` test).
3. `score_edge` is a pure function with a table-driven test: an `AGENTS.md`-sourced,
   triple-corroborated edge scores meaningfully higher than a single meeting-notes-sourced
   edge — assert the actual ordering, not just "a number came back."
4. `parse_candidates` matches `triples.py`'s strictness bar: malformed lines dropped,
   candidates without resolvable evidence dropped, count capped, no exception ever escapes
   into the SSE stream.
5. The existing chat flow for an unambiguous question is byte-for-byte unaffected — no
   `candidates` event fires, no clarifying question appears, when only one recorded
   relationship exists.
6. Frontend: `CandidateCarousel` renders with keyboard-navigable prev/next, respects
   `prefers-reduced-motion`, and reuses the existing citation-chip component rather than a
   parallel implementation.

## 3. Test matrix

`test_graph.py`: `graph_path_candidates` — single-chain passthrough, two-materially-
different-chains case (reproduce the Connector/Nautical/Stevedore shape with seeded
fixture data at small scale), hop-cap honored per candidate.
`test_confidence.py` (new): `score_edge` ordering across kind/corroboration combinations,
meeting-notes-title heuristic true/false cases.
`test_candidates.py` (new): `parse_candidates` — well-formed block, malformed lines
dropped, cap enforced, unresolvable-evidence candidate dropped, empty/no-block input.
`test_agent_loop.py`: `agent.ask` emits a `candidates` event only when the block is present;
`ScriptedProvider` fixture extended with a candidates-block response.
`test_api.py`: `/api/chat` SSE stream forwards a `candidates` event verbatim.
Frontend: a Playwright pass (per this session's `/verify`-style live-browser convention,
not just typecheck) driving a seeded ambiguous question end-to-end and screenshotting the
carousel.

## 4. Related work folded in from the same session (do before or alongside, not instead of)

- **Repo graph refresh**: `Connector`'s own graph edges predate today's entity-resolution
  and manifest-exclusion fixes (tracked in memory as `repo-graph-refresh-pending`) — refresh
  it before relying on its edges as confidence-scoring ground truth in §B.
- **Connect Nautical and Stevedore directly** once available, rather than only inferring
  them through Connector's and Confluence's docs — likely raises both corroboration count
  and evidence-kind quality for exactly the edges this plan scores.
- Meeting-notes triple-extraction quality is *not* a separate fix in this plan — it's
  absorbed into §B's confidence weighting (lower-confidence, not filtered/deleted; the
  edge is real, it's just less authoritative than a deliberate doc).

---

## 5. Implementation prompt (paste into a fresh Claude Code session)

```
Implement Plan 06 (Multi-Angle, Confidence-Scored Answers) for QuickJoiner exactly per
docs/plans/06-multi-angle-confidence-scored-answers.md (read it and CLAUDE.md fully first).
Read section 0 carefully — it's a real, reproduced session failure, not a hypothetical.

NON-NEGOTIABLE INVARIANTS:
- The grounding contract is untouched: every candidate answer and every clarifying
  question still only ever cites real evidence already retrieved; parse_candidates must
  drop anything that doesn't resolve to a real citation, never repair/invent one — same
  posture as ingest/triples.py's parse_triples (off-vocabulary dropped, never coerced).
- graph_path (singular) keeps its exact current signature and behavior — everything
  calling it today (agent tool, /api/graph/path, the web UI's Path Finder) must keep
  working unchanged. graph_path_candidates is additive, not a replacement.
- Confidence is computed from real signals server-side (evidence-doc kind, corroboration
  count) — never trust an LLM-reported confidence number directly into the UI.
- An unambiguous question must produce byte-for-byte the same chat experience as today —
  no candidates event, no clarifying-question detour — when only one recorded
  relationship exists. This is a strict regression gate, not a nice-to-have.

PROJECT MECHANICS:
- Tests: `.venv\Scripts\python.exe -m pytest -q` from D:\QuickJoiner. Full suite must stay
  green (currently 272 passed, 10 skipped — pg-gated, 1 pre-existing unrelated failure in
  test_evals.py you do not need to fix).
- SQL is portable ?-placeholder style shared between SQLite/Postgres catalogs
  (_SqlCatalog base in memory/catalog.py) — any new query goes in the shared base, not
  SQLite-only, with a Postgres parity check in tests/test_pg_backend.py if it touches
  schema.
- Mirror ingest/triples.py's parsing strictness exactly for parse_candidates — that module
  is the house style for "LLM output that must never be trusted verbatim into structured
  state."
- SSE event plumbing: agent/agent.py's on_event callback -> api/app.py's
  `events.put({"type": etype, "data": detail})` at the chat endpoint's
  `ctx.build_agent(...).ask(...)` call site -> frontend/src/App.tsx's SSE switch (~line
  202) -> a new frontend/src/components/CandidateCarousel.tsx. Reuse CiteBook/
  renderMarkdown from components/markdown.tsx for citations inside the carousel — do not
  build a second citation renderer.
- Live-verify the frontend piece the way this session did: npm install/build only when
  told it's safe to (ask if anything else long-running might be sharing the machine),
  restart qj serve without touching any other running qj process, Playwright screenshot
  evidence, not just tsc --noEmit.

ORDER: A (graph_path_candidates + tool text + prompt guidance — ships value alone, no UI
needed) -> B (score_edge, wired into A's tool text) -> C (parse_candidates + SSE event +
CandidateCarousel).

WORKED-EXAMPLE GATE (do this last, before calling it done): against a workspace containing
this session's real Connector/Confluence data (or an equivalent seeded fixture reproducing
the exact shape — a 1-hop meeting-notes-sourced edge vs a 3-hop AGENTS.md+wiki-sourced
chain between the same two entities), ask "how are nautical and connector connected?" through
the real chat endpoint and confirm it no longer silently returns the Connector.Monitor/
"Development" tangent as an uncaveated answer. Paste the actual transcript in your final
report.

DEFINITION OF DONE:
- Full suite green, count strictly greater than before, zero regressions.
- Every acceptance criterion in the plan's §2 mapped to its covering test in your report;
  say plainly if any is not fully met.
- CLAUDE.md updated: agent/ architecture bullet (graph_path_candidates, confidence.py,
  candidates.py), the SSE event list (chat.py docstring already enumerates event types —
  add "candidates"), and a status line under "Next steps."
- The three related-work items in the plan's §4 are NOT silently done as part of this —
  call out explicitly in your report whether you did them, deferred them, or they're still
  blocked (e.g. Nautical/Stevedore not yet connectable).
```
