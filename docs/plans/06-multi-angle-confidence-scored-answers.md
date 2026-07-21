# Plan 06 — Multi-Angle, Confidence-Scored Answers + Clarifying Questions

> **STATUS: ◐ CODE SHIPPED (2026-07-17), verification + related-work open.** All four phases
> A→D→B→C landed: `graph_path_candidates` + ambiguity text, `entity_evidence`-driven adjudicator,
> `confidence.py` (`score_edge`/`score_chain`), `candidates.py` + SSE event + `CandidateCarousel`.
> **Open:** AC #7 / §1.D live check (`resolve_entity('webroot connector')` merging fires at ingest
> time — awaiting the next clean Connector + Confluence re-sync; code + fixtures done) and the §4
> related work (repo-graph refresh, connect Nautical↔Stevedore). When these close, graduate the
> substance into the architecture docs and **delete this plan** (CLAUDE.md house rule). See
> [STATUS.md](STATUS.md).

Effort: ~7–9 dev-days · Dependencies: none to start section A; C benefits from 01 (gaps) and
the repo-graph-refresh follow-up · Gate: the worked example below (Connector/Nautical/
Stevedore) must produce the *right* answer, not just *an* answer, with a covering test.

> Hardened 2026-07-17: every code reference below re-verified against the current tree;
> the open design questions (materially-different definition, `score_edge` formula, §1.D
> adjudicator context) are now closed, and §6 is the full executable implementation plan.

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
plan as related work (§4):

- The user has not yet connected Nautical or Stevedore as their own sources — QuickJoiner
  currently only knows about them through what *other* systems (Connector's docs,
  Confluence) say about them. Direct ingestion would very likely produce more specific,
  higher-confidence edges for exactly this scenario.
- Meeting-notes-style Confluence pages are a real vector for the LLM triple extractor
  (`ingest/triples.py`) to collapse a loose, multi-topic discussion into an
  over-confident, coarse `depends_on` edge that has no way to beat a deliberate
  architecture doc's edge except by winning on hop-count.

## 1. Design

### Ground-truth code map (verified 2026-07-17)

- `catalog.graph_path(src_id, dst_id, max_hops=3) -> list[dict] | None` lives in the
  backend-neutral `_SqlCatalog` base (`quickjoiner/memory/catalog.py:498-542`; class starts
  line 90, SQLite `Catalog` adapter at 690). It loads the **full edge table** via
  `_EDGE_SELECT` (line 480 — joins entity names + evidence title/uri/kind) into an
  in-memory adjacency dict, then runs an undirected node-BFS with `came_from`
  backtracking. Returns `[]` when `src == dst`, `None` when unconnected within `max_hops`.
  Its docstring explicitly commits to scanning every edge (a silent cap could turn a real
  connection into a false "not learned") — `graph_path_candidates` must keep that posture.
- The `graph_path` **agent tool** (`quickjoiner/agent/tools.py:192-217`) resolves both
  entities via `catalog.resolve_entity` (alias-aware), defaults `max_hops` to
  `_DEFAULT_MAX_HOPS = 5` (line 190 — NOT the catalog's 3), emits `NO_RESULTS` /
  `NO_PATH` (+ a retry hint when `max_hops < 8`) / a numbered hop list with
  `[evidence: title]` per hop. The hub-capping prompt-in-tool-text pattern to mirror is
  `_NEIGHBORS_FULL_LIST_MAX = 60` / `_NEIGHBORS_SAMPLE_PER_REL = 8` (lines 141-184).
- `edges` schema (catalog.py:59-65): `(src, rel, dst, evidence_doc_id, detail)`,
  PK `(src, rel, dst, evidence_doc_id)` — i.e. **corroboration is already representable as
  multiple rows per logical edge**, one per evidence doc; `replace_doc_edges`
  (catalog.py:409-420) replaces per `evidence_doc_id`. `documents` (catalog.py:30-33)
  carries `doc_id, source_id, uri, title, kind`. No schema change is needed anywhere in
  this plan — confirmed.
- Parsing house style: `quickjoiner/ingest/triples.py:36-48` `parse_triples` — one anchored
  regex (`_TRIPLE_LINE`, bounded field lengths `{1,80}`), non-matching lines silently
  skipped, vocabulary-checked, hard cap `_MAX_TRIPLES = 20`, never raises, never repairs.
  `sessions.py` only **re-exports** it (line 60) for back-compat — `ingest/triples.py` is
  the real home; `parse_candidates` mirrors this file, not sessions.py.
- Adjudicator (`quickjoiner/ingest/entity_resolution.py`): type alias
  `Adjudicator = Callable[[str, str, list[str]], str | None]` (line 43),
  `EntityResolver.resolve(entity_id, name, type_)` (line 79) calls it at line 106 with
  **bare candidate name strings only**; `make_llm_adjudicator` (lines 145-160) builds a
  prompt containing nothing but the names. The resolver is wired from
  `quickjoiner/app.py:110-133` and invoked from `pipeline._persist_graph`
  (`quickjoiner/ingest/pipeline.py:284-305`) — which today receives `(doc_id, entities,
  alias_rows, edges, source_id)` and has **no document title/kind in scope**; both its
  callers (`_sync_graph` and `_apply_triples`) do. `graph.entity_resolution` defaults
  False (`config.py:143`).
- Chat SSE plumbing: `agent/agent.py::OnboardingAgent.ask` (lines 34-99) drives the loop
  and emits `thinking|delta|tool_call` through `on_event`; the **`answer` event is emitted
  by the endpoint worker, not the agent** — `api/app.py:750-790` (`POST /api/chat`,
  forwarding lambda `on_event=lambda etype, detail: events.put({"type": etype, "data":
  detail})` at line 770, `answer` put at 774, docstring listing event types at 752). The
  worker thread already forwards *any* event type unchanged, so §C needs **zero app.py
  logic changes** (docstring only). Frontend switch: `frontend/src/App.tsx` lines 200-209
  (the plan's old "~line 202" was right). `ChatEvent` union: `frontend/src/types.ts:171-177`.
- Citation machinery to reuse: `CiteBook` / `renderInline` / `renderMarkdown` in
  `frontend/src/components/markdown.tsx` (lines 29 / 79 / 250); `Chat.tsx::AnswerBody`
  (lines 218-261) creates one `CiteBook` per answer, renders, then reads `book.refs` for
  the grounded stamp + `SourcesModal` (line 76). The carousel must share **that same
  book** so its citations join the answer's numbering and sources modal.
- Must-not-regress consumers of `graph_path`: the agent tool (above),
  `GET /api/graph/path` (`api/app.py:586`, default `max_hops=3`), and the web UI Path
  Finder (`frontend/src/components/GraphView.tsx:784`, calls `api.graphPath(a, b, 5)`).
  All three stay pointed at the **untouched** singular `graph_path`.
- `ScriptedProvider` is a class in `tests/test_agent_loop.py:5-19` (not a conftest
  fixture); it simulates streaming by replaying `result.text` as one `on_stream("text", …)`
  call — which matters for §C (the raw candidates block WILL appear in `delta` events; see
  below).

### A. Ambiguity detection + one clarifying question (ships first)

The cheapest, highest-value slice: when a relationship question has more than one
materially different recorded answer, don't silently pick — ask.

- `_SqlCatalog` grows a sibling (NOT a rewrite —
  `graph_path` keeps its exact current code; equivalence is enforced by test):

  ```python
  def graph_path_candidates(self, src_id: str, dst_id: str, max_hops: int = 3,
                            max_candidates: int = 3) -> list[list[dict]]:
  ```

  Bounded BFS over **simple paths** (see §6 Phase A for the full algorithm): same
  full-edge-table load and undirected adjacency as `graph_path`, but the frontier carries
  `(node, path_so_far)` and each non-destination node may be *expanded* up to
  `max_candidates` times (the classic k-shortest-paths relaxation of "visited"), so the
  1-hop meeting-notes edge and the 3-hop AGENTS.md chain both surface. Raw hits are
  deduped by **signature** (definition below) and the first `max_candidates` distinct
  signatures are returned, in BFS (= nondecreasing hop count) order. `[]` for src==dst
  and for no-path (the tool distinguishes those before calling). Safety bounds so a hub
  graph can't blow up: raw-path cap `4 * max_candidates`, expansion cap 20 000 path
  states — both are *candidate-search* bounds; candidate 0 is still found by exactly the
  same exhaustive BFS reachability as `graph_path`, so "no path" can never become wrong.

- **"Materially different", precisely** (closes the old open question). With
  `evidence_class(hop)` from §B's `classify_evidence` (shipped in Phase A, scoring later):

  ```
  signature(chain) = ( frozenset(intermediate entity ids),   # nodes strictly between src and dst
                       frozenset(hop["rel"] for hop in chain),
                       frozenset(evidence_class(hop) for hop in chain) )
  ```

  Two chains are **materially different iff their signatures differ**. Same-signature
  chains are duplicates (e.g. the same route re-evidenced by a second doc) — keep the
  first (shortest). This is testable, and it classifies the motivating case correctly:
  the 1-hop chain is `(∅, {depends_on}, {meeting-notes})`, the 3-hop chain is
  `({service:stevedore, …}, {depends_on}, {authored-doc, generic})` — different on all
  three components. Hop count is deliberately *not* its own component: it's implied by
  the intermediate set whenever it matters, and two same-route chains of equal shape
  shouldn't count as two answers.

- `agent/tools.py::graph_path` tool now calls `catalog.graph_path_candidates(...)` once:
  - **0 chains** → today's `NO_PATH` text, byte-identical (and `NO_RESULTS`/same-entity
    branches before the catalog call are untouched).
  - **1 chain** → today's numbered-hop text, byte-identical when every hop is healthy
    (golden-string test); if any hop's evidence classifies as `meeting-notes` (Phase A) or
    scores < 0.40 (once Phase B lands), append a caveat line — see §B.
  - **≥2 chains** → a multi-chain block, one numbered hop list per chain, each headed
    `Chain k (<h> hop(s)<, confidence 0.xx after Phase B>)`, followed by a prompt-facing
    instruction *inside the tool text* (same pattern as the `_NEIGHBORS_FULL_LIST_MAX` hub
    cap at tools.py:141-184): *"These chains are materially different (different
    intermediates / relations / evidence). State both with their evidence, or ask ONE
    short clarifying question about which the user means — do NOT present only one as the
    answer."*
- `agent/prompts.py` (insert after the existing graph_path/graph_neighbors paragraph,
  prompts.py:24-30): *"If your tools surface more than one materially different candidate
  relationship, and the question doesn't already disambiguate (e.g. 'architecturally' vs
  'organizationally'), ask ONE short clarifying question naming the specific
  interpretations before committing to an answer. Don't guess silently when the graph
  itself recorded more than one distinct claim."* A clarifying question is just a normal
  assistant turn with no tool calls — `agent.py`'s loop already returns `result.text`
  either way; no loop changes needed (verified: agent.py:53-55).
- No new config keys: ambiguity surfacing only changes graph-tool *text*, which is exactly
  as additive as the existing hub-capping text. Constants live in the modules that use them.

### B. Confidence scoring (deterministic, not LLM self-report)

Every existing invariant in this codebase computes grounding from real signals (dense
cosine, hop count, source presence) rather than trusting the model's self-assessment —
confidence scoring follows the same principle.

- New pure module `quickjoiner/agent/confidence.py` with **two** functions (the old
  single-signature sketch `score_edge(evidence_kind, source_id, corroboration_count)`
  was wrong — the meeting-notes heuristic needs the *title/uri*, and doc- vs
  source-corroboration are distinct signals):

  ```python
  def classify_evidence(title: str, uri: str, kind: str) -> str:
      """'dependency-map' | 'meeting-notes' | 'authored-doc' | 'generic' — first match wins."""

  def score_edge(evidence_class: str, doc_corroboration: int, source_corroboration: int) -> float:
  ```

  **Classifier, exact rules** (checked in this order; all case-insensitive):
  1. `dependency-map` — `uri` ends with `::dependency-map` (the synthesized doc URIs from
     `connectors/deps.py`; deterministic manifest-derived evidence).
  2. `meeting-notes` — the title *is* a date, or contains meeting vocabulary:
     ```python
     _DATE_TITLE = re.compile(
         r"^\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
         r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
         r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\s*$",
         re.IGNORECASE)
     _MEETING_TITLE = re.compile(
         r"\b(meeting notes?|minutes|retro(?:spective)?|stand-?up|weekly sync|1:1|agenda)\b",
         re.IGNORECASE)
     ```
     `meeting-notes` iff `_DATE_TITLE.match(title)` or `_MEETING_TITLE.search(title)`.
     ("Jan 6, 2026" — the motivating page — matches `_DATE_TITLE`.)
  3. `authored-doc` — the uri's basename matches
     `^(agents|readme|architecture|arch|design|adr(?:[-_].+)?|claude|contributing)\.(md|rst|txt|adoc)$`
     (case-insensitive, path/fragment stripped first).
  4. `generic` — everything else. **Honesty correction to the original plan**: it wanted
     "a service-description wiki page" to classify high, but Confluence pages ingest as
     `kind="doc"` (`connectors/confluence.py:29`) exactly like every other prose doc —
     there is no reliable signal to tell a dedicated architecture page from any other
     page. So a formal wiki page lands in `generic` (0.45 base) and earns its lift from
     **corroboration** instead of from a shape heuristic we can't actually compute.
     (In the motivating case the 3-hop chain still wins comfortably: min-hop 0.45 vs 0.25.)

  **Score formula, locked** (weights ordered by what the session demonstrated mattering):

  ```python
  _BASE = {"dependency-map": 0.60, "authored-doc": 0.55, "generic": 0.45, "meeting-notes": 0.25}

  def score_edge(evidence_class, doc_corroboration, source_corroboration):
      s = _BASE.get(evidence_class, 0.45)
      s += 0.15 * (min(max(doc_corroboration, 1), 3) - 1) / 2   # +0.075/extra doc, caps at +0.15 (>=3 docs)
      s += 0.20 * min(max(source_corroboration, 1) - 1, 1)      # +0.20 once >=2 distinct SOURCES agree
      return round(min(max(s, 0.05), 0.95), 2)                  # never 0/1: heuristic, not proof
  ```

  Range check: max = 0.60 + 0.15 + 0.20 = 0.95; a lone meeting-notes edge = 0.25. The §2.3
  ordering (AGENTS.md-sourced, triple-corroborated 0.90 ≫ single meeting-notes 0.25) holds
  by construction. **Chain confidence = min over its hop scores** (weakest link). Hop
  count contributes nothing — that is this plan's entire lesson.

- **Corroboration query, exact SQL** (closes the old open question). New read-only method
  in the neutral `_SqlCatalog` base — no new columns (the `(src, rel, dst, evidence_doc_id)`
  PK already stores one row per corroborating doc), no literal `%`/`?` in the SQL text so
  the `PostgresCatalog._pg` placeholder swap (`pg_catalog.py:34-37`) stays safe:

  ```python
  def edge_corroboration(self, src: str, rel: str, dst: str) -> dict:
      """How many distinct evidence docs, and distinct sources, assert one exact edge."""
      row = self._read_one(
          """SELECT COUNT(DISTINCT g.evidence_doc_id) AS doc_count,
                    COUNT(DISTINCT d.source_id) AS source_count
             FROM edges g LEFT JOIN documents d ON d.doc_id = g.evidence_doc_id
             WHERE g.src = ? AND g.rel = ? AND g.dst = ? AND g.evidence_doc_id <> ''""",
          (src, rel, dst))
      return {"doc_count": (row or {}).get("doc_count") or 0,
              "source_count": (row or {}).get("source_count") or 0}
  ```

  Cost discipline: `graph_path` chains call it per hop (≤ 5 hops × ≤ 3 chains).
  `graph_neighbors` does **not** run corroboration queries (a 60-row full list would mean
  60 queries; a hub would mean thousands) — it annotates from `classify_evidence` alone,
  which is free (the class inputs already ride every `_EDGE_SELECT` row).
- Tool-text annotations: `graph_path` chains get `confidence 0.xx` per chain plus, for
  any hop < 0.40, *"low-confidence: sourced only from informal meeting notes ("Jan 6,
  2026") — corroborate before relying on it"*; `graph_neighbors` (full-list branch only)
  appends `(low-confidence: meeting-notes evidence)` to `_format_edge` output for
  meeting-notes hops. So the model *states* confidence rather than presenting every
  recorded claim as equally solid — and the number it states was computed server-side.

### C. Multi-source candidate answers + a traversal UI

For the (less common) case where the user explicitly wants to see multiple flavors rather
than get one clarifying question:

- Structured output convention, parsed with exactly `ingest/triples.py::parse_triples`'s
  strictness (anchored bounded regex, off-format dropped silently, capped, never raises,
  never repairs) — a fenced ` ```candidates ` block, one line per candidate:
  `<rank>. <one-line summary> | confidence=<0.00-1.00> | sources: <semicolon-separated tags>`
  (semicolons, not commas — evidence titles routinely contain commas, e.g. "Jan 6, 2026";
  found by the Phase C tests during implementation).
  New `quickjoiner/agent/candidates.py` (pure, no I/O): `Candidate` dataclass +
  `parse_candidates(text) -> tuple[str, list[Candidate]]` (prose-with-block-removed,
  parsed candidates) + `known_refs(messages) -> set[str]` + `filter_resolvable(...)`.
  Full regex/state machine in §6 Phase C. Cap `_MAX_CANDIDATES = 5`.
- **The model's `confidence=` number is parsed for format validation and then discarded**
  — the original plan's block format survives, but forwarding that number to the UI would
  violate this plan's own third invariant (server-side confidence only). Instead, the
  graph tools record their §B chain scores into a per-request **score ledger**
  (`dict[str, float]`, normalized evidence-ref → best chain confidence) that
  `AppContext.build_agent` threads into both `build_builtin_tools` and the
  `OnboardingAgent`; a candidate's displayed confidence is the **min of its resolved
  refs' ledger scores**, or `null` (UI renders "unscored") when its refs weren't scored
  this turn. Deterministic, server-computed, zero trust in self-report.
- Evidence resolution, precisely: a candidate's `sources:` tags must resolve against the
  refs actually present in **this turn's tool results** — extracted by regex from the
  exact formats the tools emit (`[source: <label> | uri: <uri> | …]` from `search_memory`,
  tools.py:97; `[evidence: <title>]` from the graph tools, tools.py:149/215). A tag
  resolves iff, after lowercase/strip, tag ⊆ ref or ref ⊆ tag; a candidate is kept only
  if it has ≥ 1 tag and **every** tag resolves (drop-whole-line-on-any-invalid-part, same
  posture as `parse_triples`).
- `agent/agent.py`: post-processing on the final no-tool-calls turn only (the
  `if not result.tool_calls:` branch, agent.py:53-55, and the budget-exhausted final turn
  at 90-94). If a candidates block parses to ≥ 1 surviving candidate: emit
  `on_event("candidates", json.dumps([...]))` and return the prose (block stripped);
  otherwise return `result.text` untouched (**content is never lost** — a block whose
  candidates all fail validation stays in the answer as a visible code fence rather than
  silently vanishing). The whole step is wrapped so no exception can escape into the SSE
  stream. History keeps the model's full raw text (truthful record for
  compression/distill); known cosmetic consequence: re-opening a stored session shows the
  raw fence — acceptable v1, noted in §6.
- `api/app.py` chat endpoint: **no code change** — the worker's forwarding lambda
  (line 770) already ships arbitrary `on_event` types; only the endpoint docstring
  (line 752) and the `EventCallback` comment (agent.py:12-15) gain `candidates`.
  Note the `answer` SSE event is built by the worker from `ask()`'s return value
  (line 774), so returning the stripped prose is what keeps the final rendered answer
  clean; the raw block *does* stream by in `delta` events first (ScriptedProvider and the
  real providers both stream the final text) and is replaced when `answer` lands — the
  frontend already swaps `streamText` for `answer` (App.tsx:206), so this is a transient
  flash of a code fence, not a bug.
- Frontend (`frontend/src/App.tsx` SSE switch, lines 200-209; new
  `frontend/src/components/CandidateCarousel.tsx`): on a `candidates` event, patch the
  agent message with the parsed list; `Chat.tsx::AnswerBody` renders the carousel below
  the prose, **sharing its `CiteBook`** so candidate source chips are the same gold
  citation superscripts, numbered into the same `SourcesModal` — reuse
  `renderInline`/`CiteBook` from `components/markdown.tsx`, never a second citation
  renderer. Confidence badge (or "unscored"), one-line summary, prev/next + keyboard
  navigation, `motion-safe:` transitions only (reduced-motion = instant swap). Details in
  §6 Phase C.
- Grounding contract stays untouched: every candidate still must cite real evidence
  documents; `filter_resolvable` drops any candidate whose sources don't resolve to
  refs actually returned by tools this turn, the same way `parse_triples` drops
  off-vocabulary triples rather than repairing them.

### D. Entity-resolution adjudicator needs context, not bare names (blocks §B's corroboration count)

Found while verifying the Connector repo re-sync (2026-07-15), after entity resolution had
already been running in production for the full Confluence backfill plus the repo re-sync:
`resolve_entity('webroot connector')` still returns the Confluence-sourced
`project:webroot connector` unmerged from the repo-sourced `AppRiver.Connector.Web` /
`AppRiver.Connector.Unity` entities — the exact cross-source merge this feature was built to
catch never fired for its own motivating case.

Root cause hypothesis (verified against the code: `make_llm_adjudicator`'s prompt at
`entity_resolution.py:149-155` contains literally nothing but the type, the new name, and
the bare candidate name strings): asked to confirm two names denote the same real-world
thing with nothing but the strings themselves, the safer LLM behavior is to default to
`NONE` — which is very likely why "Webroot Connector" (a product nickname) never gets tied
to "AppRiver.Connector.Web" (its actual repo name) even though a human skimming the source
pages would connect them immediately from context.

This is not a cosmetic gap for this plan specifically: §B's confidence score depends on
**corroboration count** — how many independent documents assert the same edge — and that
count is computed per canonical entity id. An unmerged duplicate silently *splits* real
corroboration across two ids, undercounting both fragments. Fixing §B's scoring without
fixing this means the scores it produces are quietly wrong on exactly the multi-source
cases the plan cares most about.

**Resolved design** (was "needs its own design pass"; now concrete — full diff plan in §6
Phase D):

- Widen the adjudicator seam to carry evidence context on both sides:

  ```python
  # entity_resolution.py — replaces the 3-arg alias at line 43 (internal seam; the four
  # test lambdas in tests/test_entity_resolution.py are updated in the same change):
  # (type, name, candidate_names, new_entity_context, candidate_contexts) -> match | None
  Adjudicator = Callable[[str, str, list[str], str, list[str]], "str | None"]
  ```

- **New-entity context**: `EntityResolver.resolve` gains `context: str = ""`.
  `pipeline._persist_graph` gains `doc_title: str = ""`/`doc_kind: str = ""` threaded from
  its two call sites (`_sync_graph` has the `Document`; `_apply_triples` gets `kind` added
  to the pending tuple built at pipeline.py:243) and passes
  `context=f'mentioned in "{doc_title}" ({doc_kind})'`. The originally-wished "sentence
  the name was extracted from" is **not available** at this seam (extractors emit
  `(eid, name, type_)` tuples with no span info) — title+kind is what a human skimming the
  evidence list actually gets, ships without re-plumbing every extractor, and is the
  honest v1. Noted as a future refinement, not silently promised.
- **Candidate context**: new read-only `_SqlCatalog` method (portable SQL, no schema change):

  ```python
  def entity_evidence(self, entity_id: str, limit: int = 3) -> list[dict]:
      """Titles/kinds of evidence docs behind edges touching this entity."""
      return self._read_all(
          """SELECT DISTINCT d.title, d.kind FROM edges g
             JOIN documents d ON d.doc_id = g.evidence_doc_id
             WHERE g.src = ? OR g.dst = ? ORDER BY d.title LIMIT ?""",
          (entity_id, entity_id, limit))
  ```

  Called only on the adjudication path (candidates above the cosine floor — rare), so the
  per-candidate query cost is bounded by `_MAX_CANDIDATES = 5`.
- **Prompt snippet shape** (`make_llm_adjudicator`):

  ```
  New entity (type: project): "Webroot Connector"
    context: mentioned in "Jan 6, 2026" (doc)

  Candidates:
  - "AppRiver.Connector.Web" — evidence: "Connector/AGENTS.md" (doc); "AppRiver.Connector dependency map" (doc)
  - "AppRiver.Connector.Unity" — evidence: "Connector/README.md" (doc)

  Which candidate (if any) refers to the same real-world project? Judge from the
  evidence context as a person familiar with the org would — an informal nickname and a
  formal repo/package name are often the same thing when their evidence describes the
  same system. Reply with just that candidate's exact name, or NONE.
  ```

  `ADJUDICATE_SYSTEM` (line 139) keeps its "Never guess — reply NONE by default" spine
  and adds one sentence permitting context-based judgment. Precision risk (merge-happier
  prompt) is called out in §6's risk section; the guard is that a merge only ever adds an
  alias, never renames/deletes (entity_resolution.py module docstring) — a bad merge is
  recoverable by deleting the alias row.
- Acceptance check unchanged and already reproducible live: after this ships and the
  Connector/Confluence sources re-sync, `resolve_entity('webroot connector')` must return
  the `AppRiver.Connector.Web`-or-`.Unity` canonical id.

## 2. Acceptance criteria

1. The worked example: asking "how are nautical and connector connected?" against the real
   workspace either (a) asks a clarifying question distinguishing the two chains, or (b) if
   asked to just answer, states the Stevedore/ServiceBus chain and explicitly flags the
   informal meeting-notes edge as lower-confidence — it must never again present the
   `AppRiver.Connector.Monitor`/"Development" tangent as *the* answer with no caveat.
2. `graph_path_candidates(...)[0]` matches `graph_path(...)` (same hop count always; same
   edges on the deterministic seeded fixture) whenever a path exists, and returns exactly
   one chain when only one materially distinct chain exists. `graph_path` itself is
   byte-identical code — no regressions on any current `graph_path` test, `/api/graph/path`,
   or the GraphView Path Finder.
3. `score_edge` is a pure function with a table-driven test: an `AGENTS.md`-sourced,
   triple-corroborated edge (0.90) scores meaningfully higher than a single
   meeting-notes-sourced edge (0.25) — assert the actual ordering AND the actual values,
   not just "a number came back."
4. `parse_candidates` matches `triples.py`'s strictness bar: malformed lines dropped,
   candidates without resolvable evidence dropped, count capped at 5, LLM-reported
   confidence never forwarded, no exception ever escapes into the SSE stream.
5. The existing chat flow for an unambiguous question is unchanged: no `candidates` event
   fires, no clarifying-question guidance triggers, and the `graph_path` tool's output for
   a single healthy chain is **byte-for-byte today's string** (golden test). (When the
   single chain's evidence is weak, a caveat line is appended — that's §0's bug being
   fixed, deliberately outside the "unchanged" guarantee, which is scoped to
   single-chain + healthy-evidence.)
6. Frontend: `CandidateCarousel` renders with keyboard-navigable prev/next, respects
   `prefers-reduced-motion`, and reuses the existing `CiteBook` citation machinery (chips
   join the answer's `SourcesModal` numbering) rather than a parallel implementation.
7. §1.D: `resolve_entity('webroot connector')` merges with `AppRiver.Connector.Web`/`.Unity`
   on the live workspace after re-sync (plus the fixture-level adjudicator-context tests).

## 3. Test matrix

`tests/test_graph.py`: `graph_path_candidates` — single-chain passthrough vs `graph_path`,
two-materially-different-chains (seeded Connector/Nautical/Stevedore shape), same-signature
dedupe, hop-cap per candidate, `max_candidates` bound; tool-level: golden single-healthy-chain
string, ambiguity instruction on two chains, meeting-notes caveat on a weak single chain.
`tests/test_confidence.py` (new): `classify_evidence` true/false table (incl. "Jan 6, 2026",
"2026-01-06", "Weekly sync", "AppRiver.Nautical", `AGENTS.md`, `…::dependency-map`);
`score_edge` ordering + exact values + clamps.
`tests/test_candidates.py` (new): `parse_candidates` — well-formed block, malformed lines
dropped, cap enforced, block-stripping only on survivors, empty/no-block input;
`known_refs` extraction from real tool-output shapes; `filter_resolvable` all-tags rule;
ledger-based confidence attach (incl. `None` for unscored).
`tests/test_agent_loop.py`: `ask` emits `candidates` only when a valid block is present
(`ScriptedProvider` — a class in this same file — extended with a candidates-block script);
prose returned stripped; all-invalid block ⇒ text untouched, no event; emit path never raises.
`tests/test_api.py`: `/api/chat` SSE forwards a `candidates` event verbatim between `delta`
and `answer` (extend the `test_chat_streams_tool_calls_and_answer` pattern, line 399).
`tests/test_pg_backend.py` (env-gated on `QJ_TEST_DATABASE_URL`): `edge_corroboration`,
`entity_evidence`, and `graph_path_candidates` parity rows added to the existing
knowledge-graph round-trip (line 115). No schema change, so parity is behavioral only.
`tests/test_entity_resolution.py`: adjudicator receives contexts; context appears in the
built prompt; merge fires with context under a scripted provider where the bare-names
variant said NONE; existing lambdas updated to the 5-arg seam.
`tests/test_pipeline.py`: `_persist_graph` passes doc title/kind context to the resolver
(spy resolver).
Frontend: Playwright live pass (this repo's convention — no frontend unit suite exists;
manual/Playwright verification per the CLAUDE.md markdown-renderer note) driving a seeded
ambiguous question end-to-end, screenshotting the carousel + shared sources modal.

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
- **Entity-resolution adjudicator context gap** (§1.D): verified live (2026-07-15) that
  "Webroot Connector" still doesn't merge with `AppRiver.Connector.Web`/`.Unity` even after
  a full production re-sync with entity resolution on — this is a **prerequisite** for §B's
  corroboration count to be trustworthy, not an optional nice-to-have alongside it. Do this
  before or alongside §B, not after. (Also note `graph.entity_resolution` defaults False,
  config.py:143 — the live workspace has it on; fixtures must enable it explicitly.)

---

## 5. Implementation prompt (paste into a fresh Claude Code session)

```
Implement Plan 06 (Multi-Angle, Confidence-Scored Answers) for QuickJoiner exactly per
docs/plans/06-multi-angle-confidence-scored-answers.md (read it and CLAUDE.md fully first).
Read section 0 carefully — it's a real, reproduced session failure, not a hypothetical.
Section 6 of the plan is the step-by-step execution plan: follow it phase by phase.

NON-NEGOTIABLE INVARIANTS:
- The grounding contract is untouched: every candidate answer and every clarifying
  question still only ever cites real evidence already retrieved; parse_candidates must
  drop anything that doesn't resolve to a real citation, never repair/invent one — same
  posture as ingest/triples.py's parse_triples (off-vocabulary dropped, never coerced).
- graph_path (singular) keeps its exact current signature and behavior — its code is not
  edited at all. Everything calling it today (agent tool, /api/graph/path, the web UI's
  Path Finder) must keep working unchanged. graph_path_candidates is additive; a test
  asserts candidates[0] agrees with graph_path.
- Confidence is computed from real signals server-side (evidence-doc class, corroboration
  counts via edge_corroboration) — the LLM's self-reported confidence= token is parsed
  for validation and DISCARDED; the UI number comes from the server-side score ledger or
  renders "unscored".
- An unambiguous question must produce the same chat experience as today — no candidates
  event, no clarifying-question detour — and the graph_path tool's output for a single
  healthy-evidence chain must be byte-for-byte today's string (golden test). This is a
  strict regression gate, not a nice-to-have.

PROJECT MECHANICS:
- Tests: `.venv\Scripts\python.exe -m pytest -q` from D:\QuickJoiner. Full suite must stay
  green (currently ~324 passed, 10 skipped — pg env-gated — plus 1 pre-existing unrelated
  failure in test_evals.py you do not need to fix).
- SQL is portable ?-placeholder style shared between SQLite/Postgres catalogs
  (_SqlCatalog base in memory/catalog.py; PostgresCatalog swaps ? -> %s, so no literal
  '?' or '%' may appear in SQL text) — any new query goes in the shared base, not
  SQLite-only, with a behavioral parity check in tests/test_pg_backend.py. This plan adds
  NO schema changes (edges' (src,rel,dst,evidence_doc_id) PK already encodes corroboration).
- Mirror ingest/triples.py's parse_triples (lines 21-48: anchored bounded regex, silent
  drop, vocabulary check, hard cap, never raises) exactly for parse_candidates — that
  module is the house style for "LLM output that must never be trusted verbatim into
  structured state." Note sessions.py only re-exports it.
- SSE event plumbing: agent/agent.py's on_event callback -> api/app.py's forwarding
  lambda `on_event=lambda etype, detail: events.put({"type": etype, "data": detail})`
  (line ~770 inside POST /api/chat's worker; the answer event at ~774 is built from
  ask()'s return value, which is why ask() returns the stripped prose) ->
  frontend/src/App.tsx's SSE switch (lines ~200-209) -> a new
  frontend/src/components/CandidateCarousel.tsx. Reuse CiteBook/renderInline from
  components/markdown.tsx, sharing AnswerBody's CiteBook instance — do not build a second
  citation renderer. ScriptedProvider lives in tests/test_agent_loop.py (a class, not a
  conftest fixture).
- Live-verify the frontend piece the way this project does: npm build via the nvm PATH
  prepend in CLAUDE.md, only when told it's safe to (ask if anything else long-running
  might be sharing the machine), restart qj serve without touching any other running qj
  process, Playwright screenshot evidence, not just tsc --noEmit.

ORDER: A (graph_path_candidates + classify_evidence + tool text + prompt guidance — ships
value alone, no UI needed) -> D (entity-resolution adjudicator context fix — B's
corroboration count is computed per canonical entity id, so an unmerged duplicate like the
still-reproducing Webroot-Connector case silently undercounts real corroboration; fix this
before trusting B's numbers) -> B (score_edge + edge_corroboration, wired into A's tool
text + the score ledger) -> C (parse_candidates + SSE event + CandidateCarousel).

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
- CLAUDE.md AND README.md updated in the same change (docs-current rule): agent/
  architecture bullet (graph_path_candidates, confidence.py, candidates.py, adjudicator
  context), the SSE event lists (api/app.py's chat docstring at ~752, agent.py's
  EventCallback comment at 12-15, frontend types.ts ChatEvent — add "candidates"), and a
  status line under "Next steps."
- The related-work items in the plan's §4 are NOT silently done as part of this — call out
  explicitly in your report whether you did them, deferred them, or they're still blocked
  (e.g. Nautical/Stevedore not yet connectable). §1.D (entity-resolution context) is NOT
  optional related work — it's a correctness prerequisite for §B; your report must state
  whether `resolve_entity('webroot connector')` merges with `AppRiver.Connector.Web`/`.Unity`
  after your change, tested against the real workspace, not just a fixture.
```

---

## 6. Detailed implementation plan

Executable phase-by-phase plan (order A → D → B → C, per §5). All line numbers are as of
2026-07-17. Every SQL statement is `?`-placeholder portable and free of literal `?`/`%`
in text (the `PostgresCatalog._pg` constraint, pg_catalog.py:34-37). No schema changes
anywhere — confirmed against catalog.py:30-65.

### Phase A — `graph_path_candidates` + ambiguity in the tool text

**Files**: `quickjoiner/memory/catalog.py`, `quickjoiner/agent/confidence.py` (new),
`quickjoiner/agent/tools.py`, `quickjoiner/agent/prompts.py`, `tests/test_graph.py`,
`tests/test_confidence.py` (new), `tests/test_pg_backend.py`.

1. **`quickjoiner/agent/confidence.py` (new, pure)** — ship the classifier now (the
   materially-different signature needs it), scoring lands in Phase B:
   - `_DATE_TITLE`, `_MEETING_TITLE`, `_AUTHORED_BASENAME` regexes exactly as specified in
     §1.B.
   - `def classify_evidence(title: str, uri: str, kind: str) -> str` — order:
     dependency-map (uri suffix `::dependency-map`) → meeting-notes (title regexes) →
     authored-doc (uri basename after stripping query/fragment/trailing slash, matching
     `_AUTHORED_BASENAME`) → `"generic"`. `title`/`uri` may be `None` from LEFT JOINs
     (`_EDGE_SELECT` rows with missing evidence docs) — coerce to `""` first.

2. **`_SqlCatalog.graph_path_candidates`** (catalog.py, directly below `graph_path` at
   line 542; `graph_path`'s body is not touched):

   ```python
   def graph_path_candidates(self, src_id: str, dst_id: str, max_hops: int = 3,
                             max_candidates: int = 3) -> list[list[dict]]:
   ```

   Algorithm (bounded simple-path BFS, k-shortest-paths relaxation):
   - `if src_id == dst_id: return []`.
   - Load `rows = self._read_all(self._EDGE_SELECT)`; build the same undirected
     adjacency dict as `graph_path` (lines 512-516).
   - `raw: list[list[dict]] = []`; `expansions: dict[str, int] = defaultdict(int)`;
     `frontier = deque([(src_id, [], {src_id})])` where the third element is the
     path's node set; `budget = 20_000` popped states; `raw_cap = 4 * max_candidates`.
   - Loop: pop `(node, path, on_path)`; skip if `len(path) >= max_hops`. For each
     `edge` in `adjacency.get(node, ())`: `nxt` = far end; if `nxt in on_path`,
     continue (simple paths only); if `nxt == dst_id`, append `path + [edge]` to `raw`
     (stop when `len(raw) == raw_cap`); else if `expansions[nxt] < max_candidates`,
     increment and push `(nxt, path + [edge], on_path | {nxt})`.
   - Signature dedupe, preserving BFS order (nondecreasing hop count):
     `sig = (frozenset(intermediate node ids), frozenset(hop rels), frozenset(classify_evidence(hop) per hop))`
     where intermediates are every path node except `src_id`/`dst_id` (walk the hop
     endpoints the same way the tool formats them: the far end of each edge relative to
     the walk). Import `classify_evidence` locally inside the method (memory/ must not
     import agent/ at module level — actually place the import at module top of
     catalog.py ONLY if no cycle: `agent.confidence` imports nothing from memory, so a
     top-level `from quickjoiner.agent.confidence import classify_evidence` is
     cycle-safe; if the implementer prefers zero memory→agent coupling, move
     `classify_evidence` to `quickjoiner/ingest/` — decide once, note in CLAUDE.md.
     Recommended: put the module at `quickjoiner/agent/confidence.py` per the original
     plan and import it lazily inside `graph_path_candidates` to keep the layering
     visibly one-way).
   - Return first `max_candidates` distinct-signature chains.
   - **Reachability guarantee**: the first path BFS finds pops through exactly the states
     node-BFS would settle (every node admits ≥1 expansion), so candidate 0 exists iff
     `graph_path` finds a path — the bounds only trim *extra* candidates. State this in
     the docstring next to `graph_path`'s "unbounded on purpose" note.

3. **`agent/tools.py::graph_path` tool** (lines 192-217): replace the single
   `catalog.graph_path(...)` call with `chains = catalog.graph_path_candidates(ent_a["id"],
   ent_b["id"], max_hops, max_candidates=3)`. Branches:
   - `not chains` → the existing `NO_PATH` string, character-identical (keep the
     `retry_hint` logic verbatim).
   - `len(chains) == 1` → the existing single-path formatting loop verbatim (golden
     test pins it); after Phase B, append per-hop caveat lines for weak hops only.
   - `len(chains) >= 2` → header `f"{len(chains)} distinct recorded connections exist
     between {ent_a['name']} and {ent_b['name']}:"`, then per chain
     `f"\nChain {k} ({len(chain)} hop(s)):"` + the same numbered hop lines, then the
     ambiguity instruction paragraph from §1.A. Keep the closing "Cite the evidence
     documents…" line.
   Tool `description` (tools.py:304-309): append one sentence — "If multiple distinct
   chains are recorded, the result lists all of them; present or disambiguate per the
   system prompt."

4. **`agent/prompts.py`**: insert the §1.A clarifying-question paragraph after the
   graph_path/graph_neighbors paragraph (prompts.py:24-30).

5. **Tests** (map to §2.2, §2.5, §3):
   - `test_graph.py::test_graph_path_candidates_matches_graph_path_when_single_chain` —
     seed the existing linear fixture (reuse `test_catalog_graph_path_bfs`'s seeding at
     line 198); assert `candidates == [catalog.graph_path(src, dst)]`.
   - `::test_graph_path_candidates_surfaces_materially_different_chains` — seed the §0
     shape: `svc:connector --depends_on--> svc:nautical` evidenced by doc titled
     "Jan 6, 2026", plus `svc:connector --depends_on--> svc:stevedore` and
     `svc:nautical --depends_on--> svc:stevedore` evidenced by an `AGENTS.md`-uri doc;
     assert 2 chains, first is the 1-hop, signatures differ.
   - `::test_graph_path_candidates_dedupes_same_signature_chains` — same route
     re-evidenced by a second generic doc ⇒ still 1 candidate.
   - `::test_graph_path_candidates_honors_hop_cap_and_max_candidates`.
   - `::test_graph_path_tool_single_healthy_chain_output_is_unchanged` — golden: build
     the tool output on the pre-existing fixture and assert equality with the exact
     string format used today (constructed in the test from the fixture rows).
   - `::test_graph_path_tool_flags_two_materially_different_chains` — asserts both
     chains printed + the "do NOT present only one" instruction present.
   - `test_confidence.py::test_classify_evidence_table` — parametrized true/false table
     from §3.
   - `test_pg_backend.py`: extend `test_pg_knowledge_graph_roundtrip` (line 115) with a
     `graph_path_candidates` call over the seeded edges (parity: same chains as SQLite).

**Risk/regression callout**: the tool text changes for ambiguous inputs shift model
behavior — guarded by the golden single-chain test and by leaving `graph_path` (catalog +
`/api/graph/path` + GraphView) untouched. The path-BFS state space on hub graphs is the
main perf risk — guarded by the expansion budget and per-node expansion cap; the
worst-case behavior degrades to "fewer extra candidates", never to a wrong NO_PATH.

### Phase D — adjudicator context (prerequisite for B's corroboration)

**Files**: `quickjoiner/ingest/entity_resolution.py`, `quickjoiner/ingest/pipeline.py`,
`quickjoiner/memory/catalog.py`, `tests/test_entity_resolution.py`,
`tests/test_pipeline.py`, `tests/test_pg_backend.py`.

1. **`_SqlCatalog.entity_evidence`** — exactly the SQL in §1.D. Add to the
   `_EntityCatalog` Protocol (entity_resolution.py:46-49) as an optional capability:
   keep the Protocol minimal by adding the method; test doubles gain a stub.
2. **`entity_resolution.py`**:
   - `Adjudicator` → 5-arg alias per §1.D (line 43).
   - `EntityResolver.resolve(entity_id, name, type_, context: str = "")` (line 79):
     on the adjudication branch (line 104-115), build
     `candidate_contexts = ["; ".join(f'"{r["title"]}" ({r["kind"]})' for r in
     self.catalog.entity_evidence(cid, 3)) for cid, _ in candidates]` (guard with
     `getattr(self.catalog, "entity_evidence", None)` returning `""`s if absent, so
     older doubles don't crash) and call
     `self.adjudicate(type_, name, [names], context, candidate_contexts)`.
   - `make_llm_adjudicator` (lines 145-160): build the §1.D prompt shape; empty contexts
     degrade to the current bare-names prompt lines (no "context:" / "— evidence:" text
     when empty). Extend `ADJUDICATE_SYSTEM` with the one context-judgment sentence;
     keep "Never guess — reply NONE by default."
3. **`pipeline.py`**:
   - `_persist_graph(self, doc_id, entities, alias_rows, edges, source_id,
     doc_title: str = "", doc_kind: str = "")` (line 284): pass
     `context=f'mentioned in "{doc_title}" ({doc_kind})' if doc_title else ""` into
     `resolve`.
   - `_sync_graph` call site (line 246) passes `doc.title`, `doc.kind`; the pending
     tuple (line 243) gains `doc.kind`, and `_resolve_pending_triples`/`_apply_triples`
     (lines 248-282) thread it through to `_persist_graph`.
4. **Tests**:
   - `test_entity_resolution.py`: update the four existing adjudicate lambdas to 5-arg
     (`lambda type_, name, cands, ctx, cctxs: …`); add
     `::test_adjudicator_receives_contexts` (spy adjudicator asserts the context strings
     arrive), `::test_make_llm_adjudicator_prompt_contains_evidence_context` (capture the
     provider's `messages`, assert titles present), and a scripted webroot-shaped case:
     provider replies the candidate name; assert alias added to the canonical id.
   - `test_pipeline.py::test_persist_graph_passes_doc_context_to_resolver` — spy
     resolver records `context`; ingest one titled doc with graph metadata; assert the
     title appears.
   - `test_pg_backend.py`: `entity_evidence` round-trip row in the graph test.
5. **Live check** (implementation session, not CI): re-sync Connector + Confluence on the
   real workspace with `graph.entity_resolution=true`, then
   `resolve_entity('webroot connector')` — must return the repo-sourced canonical (§2.7).

**Risk/regression callout**: a merge-friendlier prompt can over-merge (precision loss).
Guards: candidates are still same-type + above the cosine floor; a merge only adds an
alias (reversible, never renames/deletes); the "reply NONE by default" spine stays. The
Adjudicator arity change is an internal seam — grep confirms the only implementations are
`make_llm_adjudicator` and test lambdas, all updated in this phase.

### Phase B — `score_edge` + corroboration wiring

**Files**: `quickjoiner/agent/confidence.py`, `quickjoiner/memory/catalog.py`,
`quickjoiner/agent/tools.py`, `tests/test_confidence.py`, `tests/test_graph.py`,
`tests/test_pg_backend.py`.

1. **`confidence.py`**: add `_BASE` + `score_edge` exactly per §1.B (with the docstring
   spelling out the weight rationale and the "retune here, one table" note), plus
   `def score_chain(hop_scores: list[float]) -> float: return min(hop_scores)`.
2. **`_SqlCatalog.edge_corroboration`** — exactly the SQL in §1.B, placed with the other
   graph reads (after `graph_neighbors`, ~line 496).
3. **`agent/tools.py`**:
   - Helper `def _hop_score(r) -> float` inside `build_builtin_tools`:
     `cls = classify_evidence(r["evidence_title"] or "", r["evidence_uri"] or "", r["evidence_kind"] or "")`;
     `c = catalog.edge_corroboration(r["src"], r["rel"], r["dst"])`;
     `return score_edge(cls, c["doc_count"], c["source_count"])`.
   - `graph_path` tool: multi-chain headers become
     `Chain {k} ({h} hop(s), confidence {score_chain(...):.2f}):`; single-chain branch
     appends, per hop with score < 0.40, the caveat line from §1.B (and remains
     byte-identical when no hop is weak — golden test still green).
   - `graph_neighbors`: in the `len(rows) <= _NEIGHBORS_FULL_LIST_MAX` branch only,
     `_format_edge` gains an optional flag arg: append
     ` (low-confidence: meeting-notes evidence)` when `classify_evidence(...) ==
     "meeting-notes"`. No corroboration queries here (cost, §1.B).
   - **Score ledger prep for C**: `build_builtin_tools(..., score_ledger: dict[str, float]
     | None = None)`; whenever a chain is scored, for each hop's evidence ref (the same
     `evidence_title or evidence_uri or evidence_doc_id` string the tool prints) do
     `ledger[ref.strip().lower()] = max(existing, chain_score)`.
4. **Tests**:
   - `test_confidence.py::test_score_edge_ordering_and_values` — table-driven (§2.3):
     `("authored-doc", 3, 2) == 0.90`, `("meeting-notes", 1, 1) == 0.25`,
     `("generic", 1, 1) == 0.45`, `("dependency-map", 3, 2) == 0.95`, clamp cases.
   - `test_graph.py::test_edge_corroboration_counts_docs_and_sources` — seed one edge
     evidenced by 3 docs across 2 sources ⇒ `{"doc_count": 3, "source_count": 2}`;
     empty-evidence edge ⇒ zeros.
   - `::test_graph_path_tool_annotates_chain_confidence_and_weak_hops` — §0 fixture:
     assert the meeting-notes chain prints the low-confidence caveat and the numeric
     ordering is stated (0.25 < the 3-hop chain's min).
   - `::test_graph_neighbors_flags_meeting_notes_edges` (full-list branch), and re-run
     of the hub test unchanged (hub branch untouched).
   - `test_pg_backend.py`: `edge_corroboration` parity in the graph round-trip.

**Risk/regression callout**: per-hop corroboration queries add SQL round-trips to
`graph_path` tool calls — bounded at 15 (5 hops × 3 chains); acceptable on both backends
(single-row aggregates over indexed columns: `idx_edges_src`/`idx_edges_dst` exist,
catalog.py:63-64). Weights are heuristic — they live in one `_BASE` table with a test
asserting exact values, so retuning is a one-line diff + test update, and scores are
clamped to (0.05, 0.95) so no edge ever reads as certain.

### Phase C — candidates block, SSE event, CandidateCarousel

**Files**: `quickjoiner/agent/candidates.py` (new), `quickjoiner/agent/agent.py`,
`quickjoiner/app.py` (`AppContext.build_agent` threading), `quickjoiner/api/app.py`
(docstring only), `quickjoiner/agent/prompts.py` (block format instruction),
`frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/components/Chat.tsx`,
`frontend/src/components/CandidateCarousel.tsx` (new), `tests/test_candidates.py` (new),
`tests/test_agent_loop.py`, `tests/test_api.py`.

1. **`quickjoiner/agent/candidates.py`** (pure; mirrors triples.py's shape):

   ```python
   @dataclass(frozen=True)
   class Candidate:
       rank: int
       summary: str
       sources: tuple[str, ...]
       confidence: float | None = None   # server-attached (ledger), never the LLM's number

   _BLOCK = re.compile(r"```candidates[ \t]*\n(.*?)\n?```", re.DOTALL)
   _LINE = re.compile(
       r"^(\d{1,2})[.)]\s+(.{1,200}?)\s*\|\s*confidence\s*=\s*"
       r"(0(?:\.\d{1,2})?|1(?:\.0{1,2})?)\s*\|\s*sources\s*:\s*(.{1,300}?)\s*$")
   _MAX_CANDIDATES = 5
   _REF = re.compile(r"\[source:\s*([^|\]]+)|\[evidence:\s*([^\]]+)\]")

   def parse_candidates(text: str) -> tuple[str, list[Candidate]]: ...
   def known_refs(messages: list[dict]) -> set[str]: ...
   def filter_resolvable(cands: list[Candidate], refs: set[str]) -> list[Candidate]: ...
   def attach_confidence(cands, refs, ledger) -> list[Candidate]: ...
   ```

   - `parse_candidates`: first `_BLOCK` match only; per line, `_LINE.match` or silent
     drop (the matched confidence group validates format and is then **discarded**);
     cap at `_MAX_CANDIDATES`; returns `(text with the block removed, cands)` when
     ≥ 1 candidate parsed, else `(text, [])` — the block is only stripped when
     something replaces it. Never raises.
   - `known_refs`: for `m["role"] == "tool"` messages, collect `_REF` captures,
     normalized `.strip().lower()`.
   - `filter_resolvable`: keep a candidate iff it has ≥ 1 source tag and **every**
     normalized tag `t` satisfies `any(t in r or r in t for r in refs)`.
   - `attach_confidence`: per candidate, resolve each tag to its best-matching ref's
     ledger score; `confidence = round(min(scores), 2)` if every tag found a ledger
     entry, else `None`.

2. **`agent/agent.py`**: `OnboardingAgent.__init__(..., score_ledger: dict[str, float] |
   None = None)`. Extract a private `_finalize(text, messages, on_event) -> str` used by
   both return sites (lines 55 and 94): inside `try/except Exception: return text`,
   run `parse_candidates` → `filter_resolvable(cands, known_refs(messages))` →
   `attach_confidence(..., self._score_ledger or {})`; if survivors and `on_event`:
   `on_event("candidates", json.dumps([asdict-shaped dict per candidate]))` and return
   the stripped prose; else return `text` unchanged. History still appends the **raw**
   `result.text` (truthful record; known cosmetic: a re-opened stored session shows the
   fence as a code block — deferred, noted). Update the `EventCallback` comment
   (lines 12-15) with `"candidates"`.

3. **`quickjoiner/app.py::AppContext.build_agent`**: create `ledger: dict[str, float] =
   {}` per call (build_agent is invoked per /api/chat request — verified app.py worker,
   api/app.py:763), pass to `build_builtin_tools(score_ledger=ledger)` and
   `OnboardingAgent(score_ledger=ledger)`.

4. **`agent/prompts.py`**: add the block-format instruction — *"Only when the user
   explicitly asks for multiple interpretations/options, you may end your answer with a
   fenced ```candidates block, one line per option:
   `<rank>. <one-line summary> | confidence=<0.00-1.00> | sources: <tags matching your
   cited sources>`. Otherwise never emit that block."*

5. **`api/app.py`**: docstring at line 752 gains `candidates`; no logic change (the
   forwarding lambda at 770 is generic — verified).

6. **Frontend**:
   - `types.ts`: `ChatEvent` union (lines 171-177) gains
     `| { type: "candidates"; data: string }`; new
     `interface CandidateItem { rank: number; summary: string; confidence: number | null; sources: string[] }`.
   - `App.tsx` SSE switch (lines 200-209): before the `answer` case,
     `else if (e.type === "candidates") patch((m) => ({ ...m, candidates: JSON.parse(e.data) as CandidateItem[] }));`
     (wrap the parse in try/catch → ignore on malformed).
   - `Chat.tsx`: `Msg` (lines 8-19) gains `candidates?: CandidateItem[]`; `AnswerBody`
     (lines 218-261) gains a `candidates` prop and renders
     `<CandidateCarousel items={candidates} book={book} />` between the prose `blocks`
     div (line 249) and `MessageActions` — **after** `renderMarkdown` has populated the
     book, so carousel citations continue the numbering; `refs = book.refs` must be read
     **after** the carousel's chips render — restructure so the carousel's
     `renderInline` calls happen during the same render pass before `refs` is consumed
     (compute carousel nodes eagerly above the `return`).
   - `CandidateCarousel.tsx` (new):
     `({ items, book }: { items: CandidateItem[]; book: CiteBook })`. One card visible;
     header `Candidate {i+1} of {items.length}` + confidence badge
     (`{(c.confidence*100).toFixed(0)}%` in gold token, or `unscored` in `text-faint`
     mono); summary via `renderInline(c.summary, book, key)`; source chips via
     `renderInline(c.sources.map(s => `[${s}]`).join(" "), book, key)` so they become
     the standard gold citation superscripts feeding the shared `SourcesModal`.
     Prev/next `IconButton`-style buttons with `aria-label`, `ArrowLeft`/`ArrowRight`
     keydown on the focused container (`role="group"`,
     `aria-roledescription="carousel"`, roving `tabIndex={0}`); slide transition only
     under Tailwind `motion-safe:` variants (reduced-motion users get an instant swap) —
     §2.6.
   - Build: `npm run build` per CLAUDE.md (nvm PATH prepend; 2–5 min, don't kill early).

7. **Tests** (map §2.4, §2.5, §3):
   - `test_candidates.py`: `::test_parse_well_formed_block_strips_prose`,
     `::test_malformed_lines_dropped_silently`, `::test_cap_at_five`,
     `::test_all_invalid_block_leaves_text_untouched`, `::test_no_block_passthrough`,
     `::test_known_refs_extracts_tool_output_shapes` (feed literal `search_memory` /
     `graph_path` output strings from tools.py formats),
     `::test_filter_resolvable_requires_every_tag`,
     `::test_attach_confidence_min_of_ledger_or_none`.
   - `test_agent_loop.py`: `::test_ask_emits_candidates_event_with_valid_block`
     (ScriptedProvider script: tool round returning a `[source: X | uri: u | …]` result,
     then final text with a block citing X; assert one `candidates` event, prose
     stripped, event JSON has `confidence: null` without ledger and the scored value
     with a seeded ledger); `::test_ask_without_block_emits_no_candidates_and_returns_
     text_verbatim` (§2.5); `::test_candidates_postprocessing_never_raises`
     (monkeypatched parse that throws ⇒ answer unchanged, no event).
   - `test_api.py`: `::test_chat_forwards_candidates_event` — extend the
     `fake_build_agent` pattern (line 399); assert event order
     `[…, "candidates", "answer", "done"]` and that `data` round-trips verbatim.
   - **Playwright live verify** (per repo convention; no frontend unit suite): seeded
     ambiguous workspace, ask via the real UI, screenshot the carousel, click a chip →
     shared sources modal opens with unified numbering; toggle
     `prefers-reduced-motion` emulation and confirm instant swap.

**Risk/regression callout**: the biggest regression surface is §2.5 — guarded three ways:
no-block passthrough returns `result.text` reference-equal; the `candidates` event only
exists when survivors exist; app.py untouched. The known transient (raw block visible in
`delta` stream until `answer` replaces it) is cosmetic and matches existing behavior of
streamed-then-replaced text (App.tsx:206). The prompt instruction gates block emission on
explicit user request, so ordinary questions shouldn't even produce a block; a model that
emits one anyway degrades to a rendered code fence (if invalid) or a correct carousel (if
valid) — never a crash.

### Docs (every phase, same change — CLAUDE.md house rule)

- `CLAUDE.md`: agent/ bullet gains `confidence.py`/`candidates.py`/`graph_path_candidates`/
  score ledger; memory/ bullet gains `edge_corroboration`/`entity_evidence`/
  `graph_path_candidates`; ingest bullet notes the 5-arg adjudicator + context threading;
  SSE event lists updated; "Next steps" status line.
- `README.md`: user-facing note under the chat/graph features (ambiguous relationships
  ask a clarifying question; confidence stamps; candidates carousel).

### Open risks / assumptions (could not fully verify from code)

- **Live-workspace shapes**: §0's exact entity ids (`service:connector`,
  `service:appriver.nautical`, `service:stevedore`) and the "Jan 6, 2026" page title are
  taken from the session narrative as ground truth; the fixture in `test_graph.py`
  reproduces the *shape*, and the worked-example gate runs against the real workspace at
  implementation time. If the live ids differ, only the gate transcript is affected, not
  the design.
- **Same-type only** (measured 2026-07-20, after this plan shipped): resolution buckets
  candidates by type (`EntityResolver._bucket` → `entities_by_type`), so §1.D can only ever
  merge a `repo` with a `repo`. On the live workspace that leaves the same real-world thing
  split across `service:` / `repo:` / `pipeline:` nodes — 158 exact name matches, 18
  multi-source entities. Out of scope here and NOT a defect in this plan; tracked as
  **AI_ROADMAP #24 cross-source identity bridge (`same_as`)**. Relevant when running §2.7's
  live check: a NONE result there may be this boundary rather than adjudicator weakness.
- **Adjudicator efficacy**: §1.D assumes title/kind context is enough signal for the
  configured model (gemma4:cloud) to say "Webroot Connector" == "AppRiver.Connector.Web".
  If the live check still returns NONE, the next escalation is including the mention
  sentence (requires extending the extractors' `(eid, name, type_)` tuples with a span —
  explicitly out of scope here) — the plan fails visibly at §2.7 rather than silently.
- **Weight tuning**: `_BASE` and the corroboration bonuses are principled but untuned
  against a corpus; plan 05's eval runbook is the vehicle to validate them. They are
  isolated in one table with exact-value tests precisely so retuning is cheap.
- **`memory` → `agent` import direction** for `classify_evidence` inside
  `graph_path_candidates` is handled with a lazy import; if the team prefers strict
  layering, relocating the module is a rename, not a redesign.
- **Session-reload cosmetics** (§C): stored history keeps the raw candidates block;
  re-rendered old sessions show it as a code fence. Accepted v1; a client-side parse on
  history load is a follow-up, not part of this plan.
- Suite baseline assumed at ~324 passed / 10 skipped (pg env-gated) / 1 pre-existing
  unrelated `test_evals.py` failure, per the 2026-07-17 hardening pass; re-baseline at
  implementation start (this plan does not run the suite).
