# Knowledge Graph Layer — Design

Status: **Phase A SHIPPED 2026-07-10** (entities/aliases/edges tables in both
catalogs, dependency + ticket extractors in the ingest pipeline, `graph_neighbors`
agent tool, `GET /api/graph`; verified on SQLite and live Postgres).
Implementation notes vs this design: edges are replaced **per evidence document**
(`replace_doc_edges`; `delete_document` cascades) rather than per
(source, extractor) — the edge set follows the document lifecycle, which the
hash-dedupe pipeline already manages. Extractors ride on `Document.metadata["graph"]`
(deps.py) plus a pipeline-level ticket-key regex over every ingested doc.
**Phase B SHIPPED 2026-07-11**: `graph_path` (undirected BFS, hop-capped, evidence
per hop) as catalog method + agent tool + `GET /api/graph/path`; connector metadata
entities (Jira issues assert ticket→part_of→project/epic; Octopus asserts service
entities and service→deploys→environment from the dashboard); and the React
"Knowledge" view (`frontend/src/components/GraphView.tsx`: d3-force layout,
type-colored nodes, pan/zoom, click → evidence panel with citation chips,
alias-resolving search, TopBar toggle) — verified live in Chrome.
**Phase C SHIPPED 2026-07-11**: the compression/distill prompt emits a
RELATIONSHIPS section (`type: name | relation | type: name`); `parse_triples`
validates strictly against type/relation vocabularies (off-vocabulary lines are
dropped, never invented, capped at 20); accepted triples ride the
`conversation://` document as graph metadata, so the pipeline persists them
with the conversation as citable evidence. Keyless mode skips triples — the
graph stays deterministic-only without an LLM. All three phases complete.

## Problem

Cross-source correlation today is implicit: it works when retrieval happens to
surface the linking evidence and the LLM chains follow-up searches (multi-hop).
The dependency-map documents make the common case ("repo A uses repo B's
package") reliably retrievable, but three gaps remain:

1. **Determinism** — "how are A and B related?" should be a lookup, not a bet
   on top-k ranking. Long chains (A → B → C's Jira epic → deploy pipeline)
   compound retrieval misses.
2. **Visualization** — there is no way to *see* what qj knows: which sources
   overlap, what connects to what, where the knowledge is thin.
3. **Reverse traversal at scale** — "who consumes package X?" across 50 repos
   means ranking 50 dependency maps; an edge table answers it exactly.

## Non-goals

- Not a general ontology or LLM-built graph. Edges come from **deterministic
  extractors** over data we already ingest; LLM extraction is an optional,
  clearly-marked enrichment.
- Not a second source of truth for answers. The graph *routes* the agent to
  evidence; answers still cite documents and the grounding contract
  (`min_score` on dense cosine, refuse otherwise) is untouched.
- No new storage engine. Three tables in the existing catalog (SQLite and
  Postgres via the shared `_SqlCatalog` SQL base).

## Data model (catalog tables, both backends)

```sql
CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,     -- normalized: "<type>:<lowercased name>"
    name       TEXT NOT NULL,        -- display form, e.g. "AppRiver.Nautical.Models"
    type       TEXT NOT NULL,        -- repo | package | project | service | ticket | person
    source_id  TEXT NOT NULL DEFAULT ''   -- connector that first asserted it
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias      TEXT NOT NULL,        -- lowercased: "nautical models"
    entity_id  TEXT NOT NULL,
    PRIMARY KEY (alias, entity_id)
);

CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,        -- entities.id
    rel        TEXT NOT NULL,        -- depends_on | provides | references | part_of | deploys
    dst        TEXT NOT NULL,        -- entities.id
    evidence_doc_id TEXT NOT NULL DEFAULT '',  -- catalog documents.doc_id that proves it
    detail     TEXT NOT NULL DEFAULT '',       -- e.g. "3.2.0 via src/Api/Api.csproj"
    PRIMARY KEY (src, rel, dst, evidence_doc_id)
);
```

Notes:
- `evidence_doc_id` points at an ingested document (usually the dependency map
  or the raw manifest), so every edge is citable and every graph answer can be
  grounded the same way search answers are.
- Aliases reuse `connectors/deps.aliases()` — the same org-naming convention
  ("appriver.nautical" ⇒ "nautical") used in the dependency-map text, so the
  graph resolves the same loose names retrieval does.
- Edges are **replace-on-sync per (source, extractor)**: a sync deletes the
  edges it previously asserted and rewrites them (same idempotency model as
  document ingestion). Sharing model: edges are communal knowledge, like
  ingested documents — visibility gating stays at the connector/tool layer.

## Population (extractors, in priority order)

1. **Dependency extractor** (Phase A): `deps.scan_tree()` already produces the
   structure — the same sync that writes the dependency-map document also
   writes `repo --provides--> package` and `repo --depends_on--> package`
   edges plus aliases. Zero new parsing; ~30 lines in the pipeline/connectors.
2. **Ticket references** (Phase A): commit-history documents and PR/issue
   documents already contain ticket keys (`NAUT-123`); a regex extractor emits
   `repo --references--> ticket` and `ticket --references--> repo`.
3. **Connector metadata** (Phase B): Jira projects, ADO repos/pipelines,
   Octopus projects each register themselves as entities on sync
   (`service --deploys--> repo` where derivable).
4. **Distill-time LLM extraction** (Phase C, optional): `sessions.distill`
   already extracts durable facts; extend the prompt to also propose
   `(src, rel, dst)` triples, stored with `evidence_doc_id` = the
   conversation doc. Keyless mode skips this — graph stays deterministic-only.

## Query surface

**Agent tools** (registered in `agent/tools.py`, next to `search_memory`):

- `graph_neighbors(entity: str, relation?: str)` — resolves `entity` through
  `entity_aliases` (so "nautical models" works), returns edges grouped by
  relation with `detail` and evidence doc titles/uris. The system prompt gains
  one line: *"For questions about how projects/packages/services relate, call
  graph_neighbors first, then search_memory on the evidence."*
- `graph_path(a: str, b: str, max_hops: int = 3)` — BFS over `edges`,
  returns the chain with evidence per hop, or "no known path" (which the
  agent reports as not-learned, keeping refusal honest).

Both tools return **evidence citations, not prose**, so the agent must still
retrieve and cite documents for claims — the graph never becomes an
uncited answer channel.

**API + UI** (the visualization ask):

- `GET /api/graph?entity=&type=&depth=` → `{nodes: [...], edges: [...]}`
  honoring the caller's source visibility.
- React "Knowledge" view (rail entry next to Chat): force-directed graph
  (d3-force, self-hosted like the fonts), nodes colored by entity type using
  the existing token palette (teal=repo, gold=package/provenance,
  violet=unresolved), click → side panel listing evidence documents with the
  same citation chips Chat uses. Search box resolves aliases.
- Complementary later: a semantic map (2D projection of chunk embeddings,
  colored by source) for corpus-coverage inspection — orthogonal to the graph
  and can ship independently.

## Testing

- Extractor unit tests on temp trees (same fixtures as `tests/test_deps.py`).
- Graph tool tests: seed edges → `graph_neighbors`/`graph_path` resolve
  aliases and return evidence ids; unknown entity → not-learned phrasing.
- Parity: catalog tests run on SQLite; `tests/test_pg_backend.py` gains the
  same seed/query round-trip (portable SQL keeps this cheap).
- Eval case (per CLAUDE.md next steps): "does project A use project B, and
  where is that deployed?" — passes only if the answer cites dependency-map /
  deploy evidence.

## Phasing & effort

- **Phase A** (tables + dependency/ticket extractors + `graph_neighbors` +
  `/api/graph`): ~1 session. Delivers deterministic A↔B correlation and the
  data behind the visualization.
- **Phase B** (connector metadata entities + `graph_path` + React graph view):
  ~1 session. Delivers the visualization.
- **Phase C** (distill-time triples): small, optional, needs an LLM configured.
