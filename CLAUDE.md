# QuickJoiner

Onboarding intelligence system for a new-joinee principal software developer: connectors pull
data from an org's systems (code, PM tools, wikis, CI/CD, log platforms), ingest it into a
local vector memory, and a tool-calling agent answers questions **only from learned knowledge**
with citations — or says "I haven't learned that yet."

## Dev environment (this machine)

- **Never use system Python** (only 3.7/3.8 32-bit installed). Use the project venv:
  - Run: `.venv\Scripts\python.exe`, `.venv\Scripts\qj.exe`
  - Tests: `.venv\Scripts\python.exe -m pytest -q`
  - Install deps after editing pyproject.toml:
    `%USERPROFILE%\.local\bin\uv.exe pip install -e ".[dev,browser]" --python .venv\Scripts\python.exe`
- Ollama 0.31.1 installed (CLI at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`, not on PATH in
  fresh shells), signed into ollama.com free tier. Models: `gemma4:cloud` (proxies gemma4:31b,
  tools+vision, 256K ctx) and local `qwen3:4b`. Live workspace: `~/.quickjoiner/default`
  (provider ollama, model gemma4:cloud). First live run 2026-07-07 validated grounded+cited
  answers AND "I haven't learned that yet" refusal. Still no `ANTHROPIC_API_KEY`; scripted-provider
  tests remain the coverage for the Anthropic path. Playwright IS installed (playwright 1.61.0 +
  Chromium 149, verified headless launch 2026-07-07) — `qj browser login` works.
- **Node for the frontend**: nvm-windows has v22.23.1 but it's NOT on PATH (symlink never
  activated). Prepend per command: `$env:Path = "$env:LOCALAPPDATA\nvm\v22.23.1;$env:Path"`,
  then `npm run build` in `frontend/` (slow machine: build ≈ 2–5 min; don't kill it early).

## Commands

```
qj init <org> [--provider anthropic|ollama|litellm]  # create workspace (~/.quickjoiner/<org>)
qj learn <path|url|"free text fact">          # ad-hoc ingestion / taught notes
qj connect <type> --name N -o key=value ...   # register a source (types: files git github gitlab
    [--share | --private]                     #   jira confluence azure_devops octopus grafana
                                              #   datadog dynatrace elastic web_scrape).
                                              #   --private (signed-in default) keeps it to you;
                                              #   --share exposes it to everyone.
qj users add|list / qj login / qj logout / qj whoami  # auth: first `users add` turns auth ON;
                                              #   token in <workspace>/.session. Open mode until then.
qj sync [name] / qj test <name>               # incremental pull / credential check
qj ask "..." [--provider ollama] [-f html|csv|pptx|md] [--no-stream]  # grounded Q&A (streams by default)
qj chat [--project P] [--session ID|--resume]  # persistent sessions; auto-compresses history
qj projects create|list                        # purposeful conversation groups (framing + memory scope)
qj sessions list|distill <id>                  # inspect sessions / extract facts into memory
qj serve / qj status / qj sources
qj brief <architecture|week1|roadmap|quick-wins>  # cited onboarding brief from memory
qj browser login <url> / qj browser status    # Playwright profile for user-credential fallback
qj eval <set.yaml> [--init] [--agent]         # grounding evals: retrieval metrics always
                                              #   (recall@k, MRR, threshold, refusal accuracy);
                                              #   --agent adds end-to-end behavior checks (needs LLM)
```

Workspace layout: `catalog.db` (SQLite — **config settings + connector sources now live here**,
not a YAML file), `lancedb/` (vectors), `repos/` (git clones). A legacy `config.yaml` is
auto-migrated into SQLite on first load and renamed `config.yaml.migrated`.
Override location with `--workspace` or `QJ_WORKSPACE`.

Docker: `docker compose up` (or `docker build` + `docker run -p 8787:8787 -v qj-data:/data`).
All state persists in the `/data` volume; first-boot provider defaults via `QJ_PROVIDER`, then
tune everything from the UI. Lean image by default; `--build-arg WITH_BROWSER=1` bundles Playwright
for the web_scrape browser fallback. Host Ollama reachable at `host.docker.internal:11434`.

## Architecture

- `quickjoiner/llm/` — provider abstraction (`factory.create_provider` on `llm.provider`:
  `anthropic` | `ollama` | `litellm`). **Neutral message format** documented in
  `llm/base.py` (`user` / `assistant`+tool_calls / `tool` dicts); each provider converts to its
  wire format. Anthropic default model: `claude-opus-4-8`. Ollama via native `/api/chat`.
  **`litellm_provider.py`** speaks the OpenAI Chat Completions wire format over httpx to
  `{llm.base_url}/chat/completions` — point `base_url` at a LiteLLM proxy (e.g.
  `http://localhost:4000`) to reach any of its 100+ backends through one config (works with any
  OpenAI-compatible endpoint: vanilla OpenAI, vLLM, LocalAI, …). Bearer key read from the env var
  named by `llm.api_key_env` (default `LITELLM_API_KEY`; unset ⇒ no auth header for keyless local
  proxies — the secret is never stored). Tool-call `arguments` are JSON strings on the wire
  (streamed fragments merged by `index`); `reasoning_content` surfaces on the `thinking` channel.
  Pure `_to_wire`/`accumulate_delta`/`_tool_calls_from_*` are unit-tested and an injectable
  httpx transport enables MockTransport round-trip tests (`tests/test_litellm_provider.py`).
  All providers stream via `chat(..., on_stream=(kind, delta))` with kind `text|thinking`;
  extended thinking is config-gated (`llm.thinking`, `llm.thinking_budget`). Anthropic signed
  thinking blocks ride on assistant history messages as `thinking_blocks` and are re-emitted
  FIRST in `_to_wire` (API requirement during tool use).
- `quickjoiner/memory/` — **pluggable persistence (Phase 1 cloud groundwork):** `factory.py`
  (`create_catalog`/`create_store`) picks the backend on `DATABASE_URL` — unset ⇒ on-prem
  SQLite + LanceDB files; a `postgres://` DSN ⇒ cloud `PostgresCatalog` (`pg_catalog.py`) +
  `PgVectorStore` (`pg_store.py`). `catalog.py` holds a backend-neutral `_SqlCatalog` base (all SQL,
  `?` placeholders, `ON CONFLICT … excluded` — portable to both engines); `Catalog` is the SQLite
  adapter, `PostgresCatalog` the psycopg-pool adapter (`?`→`%s`). `base.py` has the `CatalogBackend`/
  `StoreBackend` Protocols. `PgVectorStore` mirrors `KnowledgeStore` on a pgvector `chunks` table
  (HNSW cosine). Cloud deps are the `cloud` extra; `docker-compose.cloud.yml` runs app+pgvector.
  Postgres path verified by `tests/test_pg_backend.py` (env-gated on `QJ_TEST_DATABASE_URL`,
  incl. a SQLite-parity retrieval test). `store.py` (LanceDB, cosine; score = 1 − distance), `catalog.py`
  (SQLite: **workspace config in a `settings` table, connector sources in the `sources` table**
  (columns owner/shared/configured/sync_interval), document hashes, sync state, users/tokens;
  `load_config`/`save_config`/`list_source_configs`/`write_source` are the config API, with
  one-time YAML migration), `embedder.py` (fastembed bge-small default; `FASTEMBED_CACHE_PATH`
  pins the model cache). `config.py` is now **models + `load_env` only** (no file persistence).
  `sources.configured=1` = a real connector; `=0` = an ingestion bucket (taught notes, webhook
  pushes) excluded from `list_source_configs`. `upsert_source` (ingest path) preserves ownership;
  `write_source`/`save_config` (config path) set it.
  **Hybrid retrieval (both stores, on by default via `retrieval.hybrid`):** dense cosine leg +
  sparse exact-token leg — FTS5 sidecar `<workspace>/fts.db` (porter, BM25, auto-backfilled from
  LanceDB on first open) for `KnowledgeStore`; a generated `tsvector` column + GIN index for
  `PgVectorStore` — fused with reciprocal rank fusion (`hybrid.py`, `rrf_k=60`,
  `candidate_multiplier=4`). **Grounding contract: `SearchHit.score` is ALWAYS the dense cosine
  and `min_score` gates on it** — fusion changes what surfaces and in what order, never the
  grounded-vs-refuse decision (sparse-only hits get their cosine via a targeted lookup).
  Cross-encoder second stage (`reranker.py`, `retrieval.reranker="fastembed"`, **on by default**;
  lazy — the ~80MB ONNX model loads on first `rank()`, not at construction, so startup/build_context
  is free; failures degrade to RRF order; `QJ_DISABLE_RERANKER=1` env kill-switch, set by the test
  suite to stay offline). `factory.create_store` wires `retrieval` + reranker into both stores.
  `KnowledgeStore.ensure_ann_index()` builds a LanceDB IVF index past `retrieval.ann_min_rows`
  (pipeline calls it after each ingest batch). **Graph-expansion retrieval** (`retrieval.graph_expansion`,
  on): `catalog.graph_expand(seed_doc_ids)` finds documents one knowledge-graph hop from the grounded
  hits (via shared entities) and `search_memory` appends them as a "RELATED via knowledge graph"
  section — the multi-hop / cross-source channel. It runs ONLY when there are already grounded hits,
  so it never turns a refusal into an answer (grounding gate untouched).
- `quickjoiner/ingest/` — `pipeline.py` (**normalize → sha256 dedupe → chunk → embed → upsert;
  idempotent**; optionally injected a `triple_extractor`), `chunkers.py` (markdown/code/prose aware;
  large markdown sections carry their heading onto every sub-chunk), `normalize.py` (NFKC + typographic
  folding: curly quotes/dashes/NBSP/zero-width/CRLF → plain ASCII, applied to doc text before
  hashing and to queries in both stores — cosmetic variants dedupe instead of re-embedding;
  pre-existing docs re-ingest once when their hash changes, then settle).
  **Contextual chunking** (`retrieval.contextual_chunks`, on in production; off when the pipeline is
  built without a retrieval config, e.g. direct construction in tests): `pipeline.breadcrumb()`
  prepends `[source · title · path]` to each chunk before embedding+indexing, so a chunk's vector
  carries the provenance/structure it was chunked away from. **Structural graph extraction at ingest**
  (in `_sync_graph`): `code_graph.py` emits `repo --defines--> symbol` / `repo --imports--> module`
  edges per code file (regex per language: py/js-ts/java-kotlin/c#/go; call graphs are out of scope —
  need tree-sitter); `triples.py` holds the shared triple vocab + `parse_triples` + `triples_to_graph`
  (**re-exported from `sessions.py`** for back-compat) and `extract_doc_triples(provider,…)` — optional
  LLM relationship extraction over prose docs, config-gated by `graph.extract_triples` (OFF by default:
  one LLM call per qualifying doc), keyless-safe, validated against the vocab, evidence = the document.
- `quickjoiner/connectors/` — contract in `base.py`: `test()`, `sync(state) -> Iterator[Document]`,
  `tools() -> [AgentTool]` (live agent tools), `handle_event(payload)` (webhooks), and a
  `modes` flag (PULL/PUSH/LIVE/BROWSER/SCRAPE). Register with `@register`; add new imports to
  `registry._load_builtin_connectors`. Payload→Document converters are **module-level pure
  functions** so tests can hit them without HTTP mocking (see `tests/test_connectors.py`,
  `tests/test_phase4_connectors.py`). `deps.py` — **dependency mapping + semantic aliasing**:
  parses package manifests found in a synced tree and emits one synthesized
  `<uri>::dependency-map` markdown Document per repo ("X depends on / provides
  AppRiver.Nautical.Models 3.2.0 …") so cross-repo links are retrievable from either end.
  Ecosystem parity (all at the same depth): .NET (csproj/vbproj/fsproj incl. old-style
  Reference, packages.config, nuspec), Java (pom.xml with ${property} interpolation +
  parent + modules; build.gradle/.kts string/map/project notations; settings.gradle/.kts;
  gradle/libs.versions.toml version catalogs), Node/React/Angular (package.json deps/dev/
  peer/optional + workspaces; angular.json projects), Python (PEP 621 + optional-deps +
  Poetry groups, requirements*.txt, Pipfile, setup.cfg, setup.py), Go (go.mod).
  `aliases()` encodes the org naming convention — drop the org prefix/scope/group, speak the
  rest ("AppRiver.Nautical.Models" ⇒ "nautical models"; "com.appriver:nautical-models" and
  "@appriver/nautical-models" ⇒ "nautical models") — embedded as "also referred to as" text so
  loose questions retrieve the right sources. Third-party deps are only aliased when they look
  org-internal: ≥3 dotted segments in the spoken base, or sharing non-generic vocabulary
  (`_GENERIC_TOKENS` blocklist) with the repo name / provided packages — so junit, react,
  github.com/lib/pq stay silent. Wired into `files` (folder mode) and `git` syncs; `SKIP_DIRS`
  lives here (files.py re-exports). Tests: `tests/test_deps.py` incl. an end-to-end "loose
  alias query correlates consumer+provider repos" case. The structural entity/edge graph
  layer on top is **designed, not built**: `docs/KNOWLEDGE_GRAPH.md`.
  `logsearch/` (grafana/datadog/dynatrace/elastic) ingests
  inventory only — logs are queried live via tools, never vectorized. `browser/` holds the
  Playwright persistent-profile session (`session.py`, optional dep `.[browser]`) and the
  `web_scrape` connector (`scraper.py`). The scraper is a **polite, resilient browser-like
  client**: realistic Chrome headers (`browser_headers`), per-host rate limiting, retry+backoff,
  robots.txt respect (default on), and **auto-fallback to the real headless browser** when a plain
  fetch is refused (`Blocked` on 403/444/429/5xx CDN codes → `_should_fall_back` probes the start
  URL, then re-crawls via Playwright). The bare-`python-httpx`-UA default was the usual cause of
  nginx 444 / WAF 403; realistic headers alone clear most (verified live vs opentext.com). No
  CAPTCHA-solving, IP rotation, or TLS-forgery — a site that still refuses a real browser is
  respected. Injectable seams for offline tests: `_fetch_http` and `_transport` (httpx MockTransport).
  Browser hardening lives in `browser/session.py` (`DESKTOP_UA`, `_CONTEXT_OPTS`, `_STEALTH_JS`
  masking `navigator.webdriver`) — applied to both `qj browser login` and headless fetch.
- `quickjoiner/connectors/specs.py` — `FORM_SPECS` per-type field catalog (label/required/secret/
  env/list) + `connector_catalog()` (adds supported `modes`) driving the web-UI connector forms
  and capability stamps. **Keep field keys in sync with what each connector reads from `options`.**
- `quickjoiner/agent/` — grounded system prompt (`prompts.py`), built-in tools
  (search_memory/remember/list_sources + graph_neighbors/graph_path in `tools.py`),
  **operational tools = the agent-tool bridge** (`ops.py`: scrape_website /
  list_connector_types / add_connector / sync_source — same service functions as the UI
  slash commands; prompt requires explicit user confirmation before add_connector, secrets
  via env: indirection only, scrape only user-given URLs, failed connection tests are not
  saved; wired in `AppContext.build_agent`), tool-call loop (`agent.py`, max 10 rounds),
  onboarding briefs (`briefs.py`: seed queries → retrieved chunks → one-shot LLM call → saved to
  `<workspace>/briefs/` and re-ingested; refuses without hits and without building a provider).
- `quickjoiner/auth.py` — opt-in local auth. `Auth` over the catalog: PBKDF2 password hashing,
  bearer tokens (sha256-hashed at rest in `auth_tokens`), `users` table. **Open mode until the
  first user exists** (no login, everything shared = pre-auth behavior). Sharing model on
  `SourceConfig` (`owner`, `shared`): ownerless = commons; owned = owner-only unless `shared`.
  `visible()` / `can_manage()` are the gate. Ingested *knowledge* stays one communal memory;
  sharing governs who sees/manages a **connector's config + credentials** and gets its live tools.
- `quickjoiner/api/` — FastAPI (`app.py`: SSE `/api/chat`, sources, sync, search, briefs;
  `/api/auth/*` status/users/login/logout; `/api/connectors` CRUD + `/test` + `/types`;
  `GET/PATCH /api/settings` — the whole `Config` (llm/embedding/retrieval/chat/**graph**) as a
  tunable dict; `POST /api/llm/test` probes the provider with a one-token round-trip, accepting
  optional unsaved `llm` overrides so the Settings drawer can verify a proxy/model before saving,
  never persisting) + `hooks.py` (HMAC-verified `POST /hooks/{source}` push ingestion). Bearer token via
  `Authorization` header → `_user()`; secret option values masked (`MASKED`) in responses, and a
  PATCH sending the mask back keeps the stored secret. Live connector tools in chat are scoped to
  `ctx.visible_sources(user)`.
- **Web UI: `frontend/`** — Vite + React 18 + TypeScript + Tailwind ("evidence ledger" design,
  second skin 2026-07-09: borderless elevation — floating translucent blur panels (rail, drawer,
  composer) over a deep canvas with one ambient accent bloom + faint grid; pill shapes; user
  messages right-aligned; mono reserved for data/stamps/citations. Bricolage Grotesque display /
  Instrument Sans body / Geist Mono via @fontsource; teal=agent gold=provenance violet=not-learned
  tokens in `src/index.css`. NOTE: token colors are raw CSS vars — Tailwind alpha modifiers like
  `bg-surface/90` silently emit nothing; use the `fill`/`fill2`/`panel` translucent tokens instead).
  Components: `App` (state + SSE orchestration), `Chat` (provenance ledger: citations dedupe into
  a numbered sources margin on lg screens, streaming caret), `Rail`, `TopBar`, `Composer`,
  `EmptyState`, `SettingsDrawer` (account + workspace settings + connector plates/forms; the
  workspace pane exposes provider config incl. LiteLLM proxy URL + api-key env var with a
  **Test connection** button hitting `POST /api/llm/test`, and **Retrieval/Knowledge-graph
  toggles** — hybrid, cross-encoder reranker, graph-expansion, contextual chunking (ingest-time),
  and LLM triple extraction — each hinted query-time vs ingest-time; a shared `Toggle` primitive),
  `api.ts`
  (typed client + SSE reader), `ui.tsx` primitives. Build: `npm run build` → `frontend/dist`;
  dev: `npm run dev` proxies /api+/hooks to :8787. FastAPI serves the UI per `_ui_dir()`:
  `QJ_UI_DIR` env → repo `frontend/dist` → legacy `api/static/index.html` fallback (kept for
  wheel installs without the built frontend). Docker builds the UI in a node:22 stage and sets
  `QJ_UI_DIR=/app/ui`.
- `quickjoiner/export.py` — markdown → md/html/csv/pptx (`--format` on `qj ask` / `qj brief`);
  SSE chat events: `thinking` / `delta` / `tool_call` / `answer` / `error` / `done`.
- `quickjoiner/sessions.py` — `SessionManager`: persistent sessions + projects (catalog tables
  `projects` / `chat_sessions`, messages stored as JSON snapshots). Token optimization: history
  over `chat.compress_after_est_tokens` is folded into a rolling summary at a **user-turn
  boundary walking backward** (never splits assistant+tool groups, never shrinks the recent
  window); LLM summarizer with deterministic digest fallback (works keyless); persisted tool
  outputs truncated to `chat.tool_result_max_chars`. Compression/`distill` extract durable
  facts → ingested as `conversation://<project>/<session>` docs (source `conversations:learned`)
  so past conversations are searchable memory. Project name/description + rolling summary are
  injected via `build_agent(extra_system=...)`.
- `quickjoiner/evals/harness.py` — YAML eval sets; deterministic retrieval layer (no LLM) +
  agent layer (refusal phrasing via `REFUSAL_MARKERS`, citations, keywords). Reports saved to
  `<workspace>/evals/*.json` for before/after comparison when tuning threshold/embedding/chunking.
- `quickjoiner/scheduler.py` — APScheduler periodic syncs for sources with `sync_interval_minutes`.

## Conventions & gotchas

- **Grounding threshold**: `retrieval.min_score = 0.55`, empirically tuned for bge-small
  (relevant ≥ 0.64, unrelated ≤ 0.55). Retune if the embedding model changes. Borderline hits are
  passed to the LLM with scores; the prompt makes the final relevance judgment.
- Secrets in connector options support env indirection: `token=env:GITHUB_TOKEN`
  (resolved by `connectors/util.resolve_secret`). Never write literal secrets into config.yaml.
- All tool results for one assistant turn must land in a single Anthropic user message
  (handled in `anthropic_provider._to_wire`).
- LanceDB: use `db.list_tables().tables` (`table_names()` is deprecated).
- `Catalog` shares one SQLite connection across API threadpool/scheduler threads
  (`check_same_thread=False` + a lock around each execute+commit) — keep new methods locked.
- Webhook ingestion must call `catalog.upsert_source` before `pipeline.ingest`, or pushed
  docs won't appear in `/api/sources` (hooks.py does this).
- Tests use `FakeEmbedder` (`tests/conftest.py`) — no model download, deterministic vectors.
- Requirement from the user: every connector should support as many connection modes as
  possible (pull, push/webhooks, live tools, browser-with-user-credentials, scrape last).
- **Keep the docs current — always.** Any change that adds/removes/renames a feature, command,
  config field, connector, endpoint, or provider must update **both** `CLAUDE.md` (architecture
  bullet + phase/status notes) and `README.md` (user-facing setup, commands, feature sections)
  in the same change — treat stale docs as a broken build. `README.md` is written for a new user
  (setup + how to run); `CLAUDE.md` is the internal source of truth (architecture, conventions,
  status). When they would disagree, fix them, don't pick one.

## Strategy & design docs (2026-07-11, "champion team" review)

`docs/PRD.md` (stories+ACs+tests, wishlist W1–W10), `docs/MARKET_ASSESSMENT.md` (honest:
not unique as "chat over docs"; differentiators = honesty contract, evidence graph,
local-first, ramp metrics), `docs/PITCH_DECK.md`, `docs/AI_ARCHITECTURE.md` (invariants
I1–I3 + 20-item retrieval/ragless roadmap), `docs/FRONTEND_ROADMAP.md` (F0–F2),
`docs/TEST_STRATEGY.md` (>90% program, T1–T4), `docs/CLOUD_ROADMAP.md` (Y1–Y5),
`docs/design/DESIGN_VISION.md` + `orrery-prototype.html` (fog-of-war "Orrery" concept,
published as a Claude artifact). These are the authoritative roadmap references.
**Execution plans** (each with a ready-to-paste prompt): `docs/plans/` — 01 knowledge-debt
backlog, 02 retrieval quality pack, 03 Slack+Teams connectors, 04 coverage fog (01 must precede
04), **05 evaluate retrieval & correlation on a connected org** (the "decide with data" runbook
for the a–e stack + embedding-change decision; run on an org-connected machine).

## Phase status (approved plan: C:\Users\aarti\.claude\plans\happy-cuddling-sutherland.md)

1. ✅ Core (learn/ask/chat, memory, providers)
2. ✅ Connector framework + files/git/github/jira/confluence/azure_devops
3. ✅ FastAPI + web UI + scheduler + webhook receivers (verified: tests/test_api.py + live smoke)
4. ✅ gitlab/octopus/log-mining connectors, Playwright browser fallback + web_scrape, onboarding briefs

Post-phase additions (2026-07-07, all tested — suite: **89 passed**):
- Eval harness (`quickjoiner/evals/`, `qj eval`) — built to decide the fine-tuning question
  with data. Live smoke vs bge-small: relevant 0.771/0.631 (grounded), unlearned 0.523/0.483
  (refused; note 0.523 is only 0.027 under the threshold — leakage risk on big corpora).
- Streaming + extended thinking in both providers; SSE `thinking`/`delta` events; web UI and
  CLI render them live. Exports: md/html/csv/pptx via `quickjoiner/export.py`.
- Gap closers: Confluence gained LIVE (CQL search tool) + PUSH (webhook re-fetches page by id);
  ADO gained `ado_search_code` (almsearch API) + `ado_get_file`.
- Multi-hop prompt guidance (prompts.py): Jira ticket → code/PR search → CI/deploy dashboard.
- Retrieval quality upgrade (2026-07-10) — the "reranker/hybrid first" rung of the fine-tuning
  ladder, built ahead of real-corpus evals: hybrid dense+sparse search with RRF in **both**
  stores, optional cross-encoder reranker (off by default), ingest normalization, ANN index
  maintenance. Suite: **132 passed**; Postgres path re-verified live against pgvector/pg16 in
  Docker (all 8 `test_pg_backend.py` tests, incl. new sparse-rescue + dense-gate parity test).
- /scrape + artifacts + conversational connectors (2026-07-10, user request): the scraper
  gained `max_depth` (link hops from a start URL; option + FORM_SPECS field).
  `agent/scrape_report.py` synthesizes a crawl into one markdown report — LLM section
  (overview/key findings with [page title] citations/notable pages/1-3 mermaid diagrams)
  with a **keyless deterministic fallback** (page digest) and an always-present deterministic
  "Site structure" mermaid built from URL paths; saved to `<workspace>/scrapes/`.
  `POST /api/scrape` (SSE: status/delta/answer/error/done, auth-gated) crawls WITHOUT
  ingesting — learning is explicit. Web UI: `/scrape <url>` composer command (crawl log in
  the reasoning-trace box, streamed synthesis), report opens in **ArtifactModal**
  (markdown + live mermaid via lazy-loaded `mermaid` npm dep — `MermaidBlock.tsx`, parse
  failures fall back to a code block; download .md; "Learn this" → /api/learn). Chat answers
  also render ```mermaid fences (shared renderer extracted to `components/markdown.tsx` —
  the renderInline local-regex gotcha lives there now). After a scrape the UI asks
  "persist as a web_scrape connector?" (yes → create with daily sync + immediate sync).
  `/connect` = conversational connector wizard (`src/wizard.ts` state machine over
  `/api/connectors/types`: type → name → each field (secrets hint env: indirection) →
  shared? → create+test (422 → offer save-anyway) → optional sync). Suite: **154 passed**;
  frontend rebuilt. UI flows typechecked but not yet browser-driven.
- Cross-source correlation groundwork (2026-07-10, user request): manifest extensions added to
  the ingest allowlist (.csproj/.sln/.nuspec/.config/.mod/.kts/Pipfile/…), `connectors/deps.py`
  dependency maps + org-convention aliasing (see Architecture), knowledge-graph layer designed
  in `docs/KNOWLEDGE_GRAPH.md`. Same-day follow-up: ecosystem parity for Java (Maven+Gradle),
  Python, Node/React/Angular, and **teach-from-UI** — `POST /api/learn` (shares
  `agent/tools.teach_fact` with the `remember` tool and `qj learn`; no LLM round-trip) +
  `/learn <fact>` slash command in the web composer. Suite: **146 passed**.
- Prompt pipeline + knowledge graph Phase A (2026-07-10): frontend dispatch formalized as
  `frontend/src/commands.ts` (Flow interceptors → Command registry → agent chat; wizard and
  post-scrape follow-up are Flows; App.tsx keeps only the agentic stage). Knowledge graph
  Phase A shipped (see Next steps item 3 for the full inventory). Agent-tool bridge queued
  (item 4). Suite: **164 passed** + 9 pg tests live vs pgvector/pg16 in Docker.
- Knowledge-debt backlog (2026-07-13, `docs/plans/01`, PRD W2.1–W2.3): **every refusal is a
  data point about what the org needs to learn.** `agent/tools.py::search_memory` logs a gap
  (fire-and-forget — a gap-write failure never changes the tool's return) on the `NO_RESULTS`
  branch, recording best score + top-3 near-misses (a second `store.search(min_score=0.0)`).
  `catalog.gaps` table + `log_gap`/`list_gaps`/`resolve_gaps` in the neutral `_SqlCatalog`
  (both backends; pg round-trip in `test_pg_backend.py`). `quickjoiner/gaps.py` (pure logic):
  `cluster_gaps` (greedy first-fit over query embeddings ≥ `cluster_threshold`; hash-only
  privacy rows cluster by exact query_hash) + `suggest` (TERM_HINTS query-term→connector-type
  table + graph entity hints). `GapsConfig` (`enabled`, `store_queries` hash-only privacy mode,
  `cluster_threshold`) on `Config.gaps`. `GET /api/gaps` (clusters computed on read) +
  `POST /api/gaps/resolve`; `list_gaps` agent tool ("what don't you know yet?"). Web UI: Rail
  "Knowledge gaps" row with open-count badge → `GapsPanel` modal; per-cluster CTAs — **Connect
  <type>** deep-links into the /connect wizard with the type preselected
  (`startWizard(types, preselectType?)` skips the type step), **Teach** prefills `/learn `,
  **Dismiss** resolves the cluster. Suite: gaps + api-gap tests green; frontend rebuilt.
- LiteLLM provider (2026-07-13): OpenAI-compatible `/chat/completions` over httpx
  (`llm/litellm_provider.py`), selectable as `llm.provider="litellm"`; point `llm.base_url` at a
  LiteLLM proxy. Key from `llm.api_key_env` env var; injectable transport for MockTransport tests.
- Retrieval-quality + correlation pack (2026-07-13, user roadmap a–e — chosen over per-content-type
  embedding routing after debate: that fragments the vector space and multiplies the grounding gate
  without touching correlation; deferred pending evals). **(b)** Contextual chunking + reranker
  on-by-default (see ingest/memory bullets). **(c)** Code-structural graph extractor
  (`ingest/code_graph.py`): repo→defines→symbol / repo→imports→module edges per code file.
  **(d)** LLM triple extraction generalized to ingested prose (`ingest/triples.py`,
  `graph.extract_triples`, keyless-safe). **(e)** Graph-expansion retrieval (`catalog.graph_expand`,
  `retrieval.graph_expansion`) — grounded hits seed a 1-hop graph walk that surfaces cross-source
  evidence the vectors missed, without weakening the dense grounding gate. **(a)** Multi-hop eval
  set (`docs/evals/multi-hop-crosssource.yaml`) + `hops`/`hop_coverage` in the eval harness (the
  measurement loop for all of the above). Suite: **232 passed, 10 skipped** (pg env-gated).
- Settings UI for the retrieval/graph stack + LLM connection test (2026-07-13, user request):
  `GET/PATCH /api/settings` now round-trips the **`graph`** config too (was llm/embedding/
  retrieval/chat only), and `POST /api/llm/test` probes the configured provider with a one-token
  round-trip (accepts unsaved `llm` overrides so the form tests a proxy/model before persisting;
  never saves). Settings drawer gained a **Test connection** button (LiteLLM-aware hint) plus
  labelled **Toggle**s for hybrid / cross-encoder reranker / graph-expansion / contextual chunking
  (ingest-time) / graph LLM-triple extraction (ingest-time), each marked query-time vs ingest-time
  (re-sync). This closes the standing "settings-drawer toggles for the a–e knobs" item and rounds
  out the LiteLLM provider config UI (which already had proxy-URL + api-key-env fields). Frontend
  rebuilt (`frontend/dist`); new API tests in `test_api.py` (settings graph/retrieval round-trip,
  `/api/llm/test` ok + failure). Also shipped: `docs/plans/05-eval-on-connected-org.md` — the
  "decide with data" runbook (+ paste-in prompt) to A/B this whole stack on a real connected org.
  Bug fixed in the same pass: `agent/tools.teach_fact` built the note URI with `int(time.time())`,
  and since contextual chunking embeds the URI in each chunk's breadcrumb, that wall-clock token
  polluted every taught-note vector — non-deterministic near the grounding gate (a flaky
  `test_learn_endpoint_teaches_fact`) and a small persistent degradation in production. Now the
  suffix is a content sha256 (stable + idempotent); regression-guarded in `test_pipeline.py`.

## Next steps (agreed with user)

1. **Fine-tuning decision is DEFERRED pending evals on a real corpus**: connect a real org (or
   large OSS repo), write ~50 eval cases, run `qj eval`. Recommendation already given: no SLM,
   no knowledge fine-tune (breaks freshness/citations/refusal); if quality gaps appear, try
   reranker/hybrid search first, then embedding fine-tune (highest ROI), and optionally a
   behavior-tune of a small Ollama model for tool-calling discipline. Hybrid+reranker are now
   BUILT (hybrid on by default; reranker via `retrieval.reranker="fastembed"`) — evals on a real
   corpus should compare hybrid on/off and reranker on/off before touching embeddings.
2. ~~First live LLM run~~ DONE 2026-07-07 via Ollama gemma4:cloud (grounded+cited answer and
   refusal both verified live). Remaining: the multi-hop "Jira ticket implemented+deployed?"
   scenario needs real connectors — add it as an eval case once an org is connected.
3. **Knowledge graph**: ~~Phase A~~ DONE 2026-07-10 (entities/entity_aliases/edges tables in
   `_SqlCatalog` — both backends, verified live on pgvector/pg16; extractors in the pipeline:
   `Document.metadata["graph"]` from deps.py dependency maps + ticket-key regex over every
   doc (`pipeline.ticket_keys`, stoplisted); edges replaced per evidence doc,
   `delete_document` cascades; `graph_neighbors` agent tool with alias resolution + prompt
   guidance; `GET /api/graph?entity=&limit=` snapshot; tests/test_graph.py).
   ~~Phase B~~ DONE 2026-07-11: `graph_path` (undirected BFS, evidence per hop — catalog +
   agent tool + `GET /api/graph/path`); Jira issue docs assert ticket→part_of→project/epic,
   Octopus asserts service entities + service→deploys→environment; React "Knowledge" view
   (`frontend/src/components/GraphView.tsx`: d3-force static layout, type-colored nodes via
   CSS tokens, pan/zoom on viewBox, click → evidence panel with citation chips, alias search
   via `/api/graph?entity=`, TopBar Waypoints toggle) — **verified live in Chrome** incl. the
   consumer→package→provider picture and alias-focused search. Gotcha fixed there: a setState
   updater must never dereference a mutable ref (pointer-drag pan crashed the tree when
   pointerup nulled the ref before queued updaters ran). ~~Phase C~~ DONE 2026-07-11:
   compression/distill prompt emits a RELATIONSHIPS section; `sessions.parse_triples`
   validates against TRIPLE_TYPES/TRIPLE_RELS vocabularies (off-vocabulary dropped, cap 20);
   triples ride the `conversation://` doc as graph metadata → edges cite the conversation;
   keyless fallback skips triples. **All three graph phases complete.**
4. ~~Agent-tool bridge~~ DONE 2026-07-11 (`quickjoiner/agent/ops.py`, tests/test_ops_tools.py):
   scrape_website (crawl → excerpts for the agent to synthesize; deterministic report still
   saved to scrapes/; nothing auto-ingested), list_connector_types, add_connector
   (confirmation-gated by prompt; test-fail = not saved; env: secrets only), sync_source.
   Slash commands remain the deterministic fast path; both hit the same service functions.
   Live-LLM behavior (does the model actually wait for confirmation?) should be exercised
   via Ollama gemma4:cloud when convenient — scripted-provider tests cover the mechanics.

Known gaps: Anthropic live path untested (no API key; Ollama path verified live),
connectors untested against real external services (converters covered by unit tests only).
