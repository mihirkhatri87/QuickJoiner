# Plan 02 — Retrieval Quality Pack (AI_ARCHITECTURE Tier 1: #1 contextual chunks, #3 calibration, #4 alias expansion)

> **STATUS: ✅ COMPLETE (A shipped 2026-07-13; B + C shipped 2026-07-17).**
> **A. Contextual chunking** — `pipeline.breadcrumb()` + `retrieval.contextual_chunks`, on by
> default (the shipped mechanism prepends the breadcrumb to the embedded text rather than the
> plan's dual-column `index_text` design; the effect, context-enriched embeddings, is delivered).
> **B. Threshold calibration** — `evals/harness.calibrate` + `qj eval --calibrate/--apply/--compare`;
> recommends the **max-margin midpoint** of the optimal band (a refinement over the plan's
> tie-break-toward-higher, which over-jumped on thin sets), flags thin eval sets, and `--compare`
> is a non-zero-exit CI regression gate. **C. Alias query expansion** — `memory/expansion.py`
> `expand_query`, `retrieval.alias_expansion`, wired into `search_memory` + `/api/search`; reuses
> the existing `catalog.resolve_entity` (no redundant `alias_entity` method was added), 1–4-token
> windows to match real org alias shapes. Tests: `test_expansion.py` + calibrate/compare in
> `test_evals.py`; live-verified on the AppRiver graph. See [STATUS.md](STATUS.md).

Effort: ~5 dev-days · Dependencies: none · Gate: every change proves itself on `qj eval`

## 1. Design

### A. Contextual chunk enrichment (deterministic breadcrumbs)
Chunks are embedded and lexically indexed with a prepended context line; the *displayed*
text stays clean.

- New dataclass in `quickjoiner/ingest/chunkers.py`:
  `ChunkText { display: str, index: str }` and a new function
  `chunk_with_context(text, kind, *, title, source_name) -> list[ChunkText]` where
  `index = f"[{source_name} · {title} · {nearest_heading}]\n{display}"` (heading omitted
  when none; the markdown chunker already walks headings — thread the current heading
  through; code/prose kinds use `[source · title]` only). Keep the existing
  `chunk_document` as a thin wrapper returning `[c.display …]` so nothing else breaks.
- `ingest/pipeline.py`: build ChunkTexts, pass both lists to the store.
- Stores (`memory/store.py`, `memory/pg_store.py`): `upsert_document(..., chunks,
  index_texts=None)` — embed **index_texts** (fallback: chunks), FTS/tsv index
  **index_texts**, store both columns (`text` = display, `index_text`).
  - LanceDB: add `index_text` field to rows; FTS sidecar's indexed `text` column receives
    index_text (display text still returned from Lance by id — SearchHit.text unchanged).
  - Postgres: `ALTER TABLE chunks ADD COLUMN IF NOT EXISTS index_text TEXT NOT NULL
    DEFAULT ''`; the generated `tsv` column must now derive from
    `title || ' ' || COALESCE(NULLIF(index_text,''), text)`. **A generated column's
    expression cannot be altered** — migration = `DROP COLUMN IF EXISTS tsv` then re-ADD
    with the new expression, then recreate the GIN index (IF NOT EXISTS). All in the DDL
    block; idempotent.
- Grounding invariant I1 untouched: the gate still reads the dense cosine of the (now
  enriched) embedding; scores WILL shift → which is exactly why C (calibration) ships in
  the same pack, and why the old 0.55 default remains until calibrated.
- One-time effect (document in CLAUDE.md like the normalize change): existing docs
  re-embed on next content change only; provide `qj reindex` **out of scope** — instead
  the eval report tells the user whether to trigger re-sync.

### B. Threshold calibration per workspace
- `evals/harness.py`: the deterministic retrieval layer already scores every case; add
  `calibrate(results, floor=0.90) -> {threshold, refusal_accuracy, grounded_recall,
  curve:[{t, ra, gr}]}` — sweep t ∈ [0.30, 0.80] step 0.01, maximize (ra+gr)/2 subject to
  ra ≥ floor; tie-break toward the higher threshold (safer refusals).
- CLI: `qj eval <set.yaml> --calibrate [--apply]`; `--apply` writes
  `retrieval.min_score` through `catalog.save_config` and prints old→new. Report JSON
  gains a `calibration` block.
- `qj eval <set.yaml> --compare <old-report.json>`: delta table per metric with ▲▼ marks —
  the merge-gate tool every later retrieval change uses.

### C. Alias query expansion (query-side twin of ingest aliasing)
- New `quickjoiner/memory/expansion.py`: `expand_query(catalog, query) -> str` — slide
  1–3-token windows over the normalized query, look each up via a new
  `catalog.alias_entity(alias) -> {id,name} | None` (exact match on `entity_aliases` +
  case-insensitive `entities.name`); append canonical names not already present:
  `"how does nautical models auth work" → "… (AppRiver.Nautical.Models)"`. Cap 3
  expansions; skip windows that are all `_GENERIC_TOKENS`-style stopwords (reuse the set
  from connectors/deps.py — import it, don't copy).
- Wire in `agent/tools.py::search_memory` and `api/app.py::/api/search` (config flag
  `retrieval.alias_expansion: bool = True`). NOT inside the stores (they have no catalog).

## 2. Acceptance criteria
1. A query matching only breadcrumb tokens (e.g. the doc title) retrieves the chunk on
   both backends; SearchHit.text contains **no breadcrumb**.
2. Old workspaces (rows without index_text) keep working: COALESCE fallback proven by a
   test that inserts a legacy-shaped row.
3. `--calibrate` on the fixture eval pack returns a threshold meeting the floor
   constraint; `--apply` round-trips through config; curve is monotone-checked in tests.
4. `--compare` prints deltas and exits non-zero if any watched metric regressed > 2 pts
   (CI-usable).
5. "nautical models" query (alias only, no literal package tokens) retrieves the
   dependency map **with expansion on**, not with it off — proven with FakeEmbedder.
6. Full-suite eval smoke: fixture pack metrics do not regress vs the committed baseline
   JSON (`tests/fixtures/eval_smoke/baseline.json` — create it in this work).
7. Postgres tsv migration is idempotent (DDL block runs twice in a test without error)
   and hybrid sparse rescue still passes on index_text.

## 3. Test matrix
`test_chunkers.py`: ChunkText breadcrumbs (heading tracking, no-heading, code kind),
wrapper compat. `test_retrieval.py`: index-vs-display separation (both the hyphen-trick
sparse test and a new breadcrumb-retrieval test), legacy-row fallback.
`test_pg_backend.py`: migration idempotency + breadcrumb retrieval parity.
`test_evals.py`: calibrate constraints/tie-break, apply round-trip, compare exit codes.
`test_expansion.py` (new): window lookup, stopword skip, cap, no-double-append; wiring
tests through search_memory (spy on store.search arg) and /api/search.

---

## 4. Implementation prompt (paste into a fresh Claude Code session)

```
Implement the Retrieval Quality Pack for QuickJoiner exactly per
docs/plans/02-retrieval-quality-pack.md (read it and CLAUDE.md fully first). This changes
the retrieval core — treat the grounding contract as sacred.

NON-NEGOTIABLE INVARIANTS:
- I1: retrieval.min_score gates on the DENSE cosine only, before and after this change.
  Hybrid fusion (RRF, memory/hybrid.py) and any reranker only reorder. Do not touch that
  logic's semantics.
- SearchHit.text returned to users/agent must remain the clean display text — breadcrumbs
  are index-only. Citations must not grow noise.
- Both stores stay behaviorally identical: KnowledgeStore (LanceDB + FTS5 sidecar
  fts.db) and PgVectorStore (pgvector + generated tsv). Any store change lands in BOTH
  with parity tests.

PROJECT MECHANICS:
- Tests: `.venv\Scripts\python.exe -m pytest -q` from D:\Claude\QuickJoiner. FakeEmbedder
  (tests/conftest.py) hashes whitespace tokens → to prove breadcrumb retrieval, put a
  distinctive token in the title (e.g. title "Zephyr Handbook", query "zephyr") that does
  NOT appear in the body.
- LanceDB gotchas: use db.list_tables().tables; upsert deletes by doc_id then re-adds;
  the FTS sidecar insert uses named placeholders over row dicts — add index_text to the
  row dict and change the FTS-indexed text column to consume it; keep the _backfill_fts
  path working for rows lacking index_text (COALESCE-style fallback in Python).
- Postgres: the tsv column is GENERATED — you cannot ALTER its expression. The DDL block
  in PgVectorStore.__init__ must: ADD COLUMN IF NOT EXISTS index_text; then rebuild tsv
  via DROP COLUMN IF EXISTS tsv + ADD (GENERATED ... to_tsvector('english', title || ' '
  || COALESCE(NULLIF(index_text,''), text))) + CREATE INDEX IF NOT EXISTS idx_chunks_tsv
  USING gin(tsv). Prove idempotency by instantiating the store twice in a test.
- Eval harness lives in quickjoiner/evals/harness.py; reports are JSON under
  <workspace>/evals/. CLI is quickjoiner/cli.py (Typer). Follow its existing option
  style; --compare non-zero exit must use typer.Exit(code=1).
- Import the generic-token set from quickjoiner/connectors/deps.py; do not duplicate it.
- alias_entity goes in _SqlCatalog (portable ?-SQL) + CatalogBackend protocol
  (memory/base.py) + a pg parity assertion in tests/test_pg_backend.py.

ORDER: A (chunkers → pipeline → both stores + tests) → C (expansion + wiring + tests) →
B (calibrate/compare + fixture baseline + tests). After A lands, create
tests/fixtures/eval_smoke/ (tiny corpus + eval yaml + baseline.json produced by running
the harness once with FakeEmbedder) and wire a test that fails on >2pt regression.

DEFINITION OF DONE:
- Full suite green; count strictly greater than before; zero regressions.
- Postgres suite green against dockerized pgvector (docker run … pgvector/pgvector:pg16,
  QJ_TEST_DATABASE_URL=postgresql://postgres:qj@localhost:55432/qj); container stopped
  and Docker Desktop shut down afterwards.
- Run the fixture eval pack before your changes (stash baseline) and after: paste the
  --compare table in your final report. Grounded-recall must improve or hold; refusal
  accuracy must hold.
- Update CLAUDE.md: memory/ and ingest/ architecture bullets (index_text, expansion,
  calibrate/compare flags) and the min_score convention note (now calibratable).
- Report every AC (plan §2) with its covering test. If any AC is not fully met, say so
  explicitly — do not soften it.
```
