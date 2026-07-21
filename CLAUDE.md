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
  extended thinking is config-gated (`llm.thinking`; Anthropic sends **adaptive** thinking —
  the old `{"type": "enabled", "budget_tokens"}` form is a 400 on the default model Opus 4.8;
  `llm.thinking_budget` now only adds `max_tokens` headroom). Anthropic signed
  thinking blocks ride on assistant history messages as `thinking_blocks` and are re-emitted
  FIRST in `_to_wire` (API requirement during tool use).
  **Anthropic prompt caching** (`llm.prompt_cache`, ON by default; 2026-07-18): the prompt is a
  prefix match over tools → system → messages, so `_build_kwargs` places one `cache_control`
  breakpoint on the system block (caches tools+system together) and
  `_mark_cache_breakpoints` (pure, tested in `tests/test_prompt_cache.py`) puts a **moving
  breakpoint on the last block of the last message** plus an intermediate marker every
  ~15 blocks walking backward (max 3 message marks + 1 system = the API's 4-breakpoint cap;
  15 keeps every new breakpoint inside the API's **20-block cache lookback**, which a
  tool-heavy 10-round turn would otherwise outrun). Rounds 2..N of a tool loop and follow-up
  turns then read the prefix at ~0.1× input price (writes 1.25× ⇒ break-even at 2 requests —
  guaranteed whenever a tool fires). Markers are attached to **copies** of blocks — history
  `thinking_blocks` ride `_to_wire` by reference and are never mutated (cache_control is also
  invalid on thinking blocks, so they're skipped). Caveats: min cacheable prefix on Opus 4.8
  is 4096 tokens (shorter ⇒ silently uncached, no error); verify live via
  `usage.cache_read_input_tokens`, logged at DEBUG in `_to_result`. Session compression
  rewrites history and invalidates the whole prefix on the turn it fires — expected; don't
  lower `chat.compress_after_est_tokens` aggressively. **Stable-prefix discipline** (the same
  change serves all three providers): `OnboardingAgent` sorts tool specs by name so the tool
  list is byte-stable across requests/processes — feeds Anthropic explicit caching, automatic
  prefix caching on OpenAI-compatible backends behind LiteLLM, and llama.cpp/Ollama KV-cache
  prefix reuse.
  **Tool names are sanitized to OpenAI's `^[a-zA-Z0-9_-]{1,64}$` at `ToolSpec` construction**
  (`base.sanitize_tool_name` via `__post_init__`) — connector live-tool names embed the source
  name (e.g. "Appriver Octopus"), and a space breaks the gpt-oss "Harmony" tool-call wire format
  (`to=functions.<name>`), which returns HTTP 500 "unexpected tokens remaining in message header".
  Sanitizing at the single choke point keeps the request payload, the model's returned
  `tool_call.name`, and the agent's dispatch key identical. **Transient-error retry**
  (`litellm_provider`, `_MAX_ATTEMPTS=4`, exponential backoff): retries `{401,429,500,502,503,504}`
  + network errors — some fronting proxies (an overloaded internal model broker) intermittently
  reject a *valid* static key under load (observed live as the same key alternating 200/401), so a
  bounded retry smooths it; a genuinely bad key still surfaces its 401 after the capped backoffs.
  Streaming retries only the connection+status handshake (before any token reaches the caller);
  once tokens flow it's committed. `Retry-After` is honored on retryable statuses when it beats
  the backoff (numeric seconds only, capped 30s). Real client errors (400) are never retried —
  EXCEPT via **strip-and-retry** (`_strip_rejected`, pure): a 400 whose body names an optional
  enhancement (`stream_options`, `reasoning_effort`, `parallel_tool_calls`, message-level
  `reasoning_content`, content-part `cache_control`) or an impossible `max_tokens` (the observed
  "must be at least 1, got -86016" negative-budget failure) removes just that piece and retries,
  so enhancements can't hard-break a stricter backend; a 400 naming none of them raises
  immediately as before. **gpt-oss / reasoning-model enhancements (2026-07-18):** (1) **Harmony
  reasoning round-trip** — gpt-oss expects the CoT that produced a tool call passed back until
  the turn completes; the agent stores `result.thinking` as `reasoning` on tool-call assistant
  history messages, `_to_wire` re-emits it as `reasoning_content` (tool-call turns only —
  finished turns' CoT is dropped by the chat template anyway), and `sessions.messages_to_json`
  strips it on persist (live-turn plumbing, never stored). (2) **`llm.reasoning_effort`**
  (low|medium|high, None ⇒ not sent) — the cost/latency dial for reasoning backends.
  (3) **`llm.parallel_tool_calls`** (None ⇒ not sent; False forces single tool calls — Harmony's
  happy path). (4) **Usage visibility**: streams request `stream_options.include_usage`; usage
  incl. `prompt_tokens_details.cached_tokens` (the vLLM automatic-prefix-cache hit counter — the
  LiteLLM-side verification of the stable-prefix work) logged at DEBUG via `_log_usage`.
  (5) **`llm.proxy_cache_control`** (OFF) — Anthropic-style cache marker on the system message as
  a content-part extra for proxies fronting Claude. (6) One pooled `httpx.Client` reused across
  requests (one TLS handshake per tool loop, not per round). Tests: `tests/test_litellm_provider.py`.
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
  (columns owner/shared/configured/sync_interval), document hashes, sync state, users/tokens,
  `sync_events` (the rolling sync/cleanup history behind the notification menu, incl. a `kind`
  column — see `sync_manager.py`; late-added columns live in the shared `_MIGRATION_STATEMENTS`
  applied best-effort by **both** adapters on open). **`reset_knowledge(include_gaps=True)`** is the
  global memory reset: DELETEs all documents/edges/entities/aliases/graph_pending/sync_state + the
  ingestion-bucket source rows (`configured=0`) + gaps, keeping configured connectors, users,
  sessions and settings (neutral `?`-SQL ⇒ both backends; caller wipes vectors via `store.reset()`);
  `load_config`/`save_config`/`list_source_configs`/`write_source` are the config API, with
  one-time YAML migration), `embedder.py` (fastembed `BAAI/bge-small-en-v1.5` default;
  `FASTEMBED_CACHE_PATH` pins the model cache; ollama provider defaults to `nomic-embed-text`.
  **Asymmetric retrieval** (`embedding.instruct`, OFF by default): prepend the model's task
  instruction to queries vs passages — `_INSTRUCTIONS` table maps a model-name substring to
  `(query_prefix, passage_prefix)`: bge → "Represent this sentence…" on queries / none on passages,
  nomic → `search_query:`/`search_document:`, e5 → `query:`/`passage:`. Applied in `embed`
  (passages) vs `embed_query` (queries); it shifts the cosine distribution so `min_score` must be
  re-checked. bge passages are unprefixed ⇒ enabling is **query-time, no re-embed**; nomic/e5
  change passages ⇒ re-sync. `FakeEmbedder` and unlisted models stay symmetric).
  `config.py` is now **models + `load_env` only** (no file persistence).
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
  **Alias query expansion** (`memory/expansion.py`, `expand_query(catalog, query)`,
  `retrieval.alias_expansion`, on): the query-side twin of ingest-time aliasing (`connectors/deps.py`).
  Slides 1–4-token windows over the normalized query, resolves each against the knowledge graph
  (reuses `catalog.resolve_entity` — exact id/name/alias, case-insensitive), and **appends** the
  canonical entity name so a loose question ("how is connector monitor built") also retrieves docs
  indexed under the formal package name (`AppRiver.Connector.Monitor`). Longest window first (claims
  positions so sub-windows don't re-resolve a parent); skips all-generic/stopword windows (reuses
  `_GENERIC_TOKENS`); caps at 3 appends; never re-appends a name already in the query. Wired into
  `agent/tools.search_memory` (the original query is still what's logged as a gap) and
  `/api/search`; best-effort (any failure falls back to the raw query). It only ADDS canonical
  tokens, so it can surface hits the raw query missed but never invents a match from nothing — the
  dense grounding gate is unchanged.
  **Plan-06 graph reads (all portable `?`-SQL in the neutral `_SqlCatalog`, no schema change):**
  `graph_path_candidates(src, dst, max_hops, max_candidates)` — bounded simple-path BFS returning
  up to k MATERIALLY different chains (signature = frozensets of intermediates/rels/evidence
  classes; candidate 0 always agrees with the untouched `graph_path`); `edge_corroboration(src,
  rel, dst)` — distinct evidence docs + distinct sources per exact edge (the
  `(src,rel,dst,evidence_doc_id)` PK already stores one row per corroborating doc);
  `entity_evidence(entity_id, limit)` — evidence titles/kinds for adjudication context.
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
  need tree-sitter); **`pubsub.py` (2026-07-17) emits runtime-coupling edges deterministically** —
  `repo --publishes_to/subscribes_to--> topic` and `repo --stores_in--> datastore` — from per-language
  Service Bus SDK patterns (C# incl. Functions bindings/`[return: ServiceBus]`/legacy clients; Python
  `get_*_sender/receiver`; JS/TS `createSender/createReceiver`; Java builder chains matched within one
  statement + `@JmsListener`/`convertAndSend`; Go `NewSender/NewReceiverFor*`), app config
  (appsettings*/\*.config/application*.properties|yml — a Subscription* key in the same file marks the
  consumer, else `references` only; connection strings → `stores_in`), and content-detected CFN/SAM/
  serverless templates (`references`/`stores_in` only — a template proves existence, not direction).
  **Honesty rules: literals only; substitution placeholders (Octopus `#{Var}`, `%VAR%`, `${VAR}`,
  `!Ref`) are skipped, never guessed** — orgs holding real values in deployment tooling get them via
  ingesting those artifacts (Octopus variable-set extraction = roadmap #22); direction asserted only
  when the API implies it (legacy `QueueClient` deliberately ignored). Same pure contract as
  code_graph (`extract_pubsub_graph(text, uri, src_id)`), wired beside it in `_sync_graph`; always on;
  tests `tests/test_pubsub_graph.py` incl. the cross-repo publisher↔subscriber bridge deps.py can't make.
  `triples.py` holds the shared triple vocab + `parse_triples` + `triples_to_graph`
  (**re-exported from `sessions.py`** for back-compat) and `extract_doc_triples(provider,…)` — optional
  LLM relationship extraction over prose docs, config-gated by `graph.extract_triples` (OFF by default:
  one LLM call per qualifying doc), keyless-safe, validated against the vocab, evidence = the document.
  **Vocab (2026-07-17): types gained `topic` (message topics/queues/streams) + `datastore`
  (databases/caches/blob stores); rels gained `publishes_to`/`subscribes_to`/`stores_in`** — the
  runtime-coupling edges package manifests can't see (two services wired only through a Service Bus
  topic share no compile-time dependency for deps.py to find). Both LLM prompts (doc extraction +
  conversation distillation in `sessions.py`) enumerate the vocab from `ALLOWED_TYPES_LINE`/
  `ALLOWED_RELS_LINE` interpolation — the sets are the single source of truth, prompts can't drift
  (lockstep-guarded by `test_compress_prompt_vocab_in_lockstep_with_validator`). Ingest-time: new
  verbs mine prose only when `graph.extract_triples` is on + re-sync; distillation picks them up on
  every compression regardless.
  **Entity-resolution adjudicator gets evidence context (plan 06 §1.D, 2026-07-17):** the
  `Adjudicator` seam in `entity_resolution.py` is 5-arg — `(type, name, candidates,
  new_entity_context, candidate_contexts)`. `pipeline._persist_graph` threads the evidence doc's
  title/kind (`'mentioned in "<title>" (<kind>)'`) into `EntityResolver.resolve(context=)`, and the
  resolver builds per-candidate evidence summaries via `catalog.entity_evidence`; the LLM prompt
  shows both sides' evidence so a nickname ("Webroot Connector") can merge with its formal repo
  name — bare name strings alone made NONE-by-default the only safe reply and the motivating
  cross-source merge never fired. Correctness prerequisite for confidence corroboration counts
  (an unmerged duplicate splits real corroboration across two ids).
- `quickjoiner/connectors/` — contract in `base.py`: `test()`, `sync(state) -> Iterator[Document]`,
  `tools() -> [AgentTool]` (live agent tools), `handle_event(payload)` (webhooks), and a
  `modes` flag (PULL/PUSH/LIVE/BROWSER/SCRAPE). Register with `@register`; add new imports to
  `registry._load_builtin_connectors`. **Cooperative sync control**: a running sync attaches a
  `SyncControl` to the connector instance (`self._control`); connectors call `self._checkpoint()`
  in long non-yielding loops (paginating an API, walking teams) so stop/pause is honored within
  seconds, and `self._stage(name, done, total)` to report the phase + estimated % (see
  `sync_manager`/`sync_control.py`). Both are no-ops by default, so a connector that ignores them —
  or a direct/CLI construction — just works. Payload→Document converters are **module-level pure
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
  `azure_devops.py` works against **both** cloud (`dev.azure.com/{organization}`) and **on-prem
  Azure DevOps Server / TFS**: set `server_url` (host up to `/tfs`) + `collection` instead of
  `organization` and the base URL becomes `{server_url}/{collection}`; code search drops the
  separate `almsearch.*` host and serves from the same collection URL; `verify_tls=false` skips
  TLS verification for internal-CA/self-signed certs (threaded through `util.get_json`/`post_json`
  as `verify`); `api_version` is configurable (default 7.0 — Server 2022→7.x, 2020→6.0, 2019→5.0).
  Auth is unchanged: a PAT via Basic auth works for SaaS and Server 2017+ alike. `util.as_bool`
  coerces string form values (shared with `octopus`). **`util.get_json`/`post_json` retry transient
  failures** (network timeouts/resets + 429/5xx, exp. backoff, `_MAX_ATTEMPTS=4`) — a single
  `WinError 10060` blip once killed a 3.3-hour TFS sync, so all connectors now ride out gateway
  hiccups. The **build-map + branch-builds are emitted FIRST** in `sync()` (before the long, fragile
  work-item phase and wrapped in try/except) so the cheap, high-value cross-source graph always lands
  even if the work-item pull later fails.
  **Work items are ingested by team over recent sprints, not flat** — a real project is far too
  large to pull whole (AppRiver's has 304k work items). `sync` enumerates the project's teams
  (`teams` option restricts to a named subset; empty = all, paginated), and for each team ingests
  the work items in its most recent `sprints` iterations (option, default `DEFAULT_SPRINTS=10`;
  `select_recent_iterations` orders by start date and drops *future* sprints via the server's
  `timeFrame`). Global dedupe across teams, `MAX_WORK_ITEMS=8000` safety cap. Anything outside that
  slice — older items, other teams, a specific #id — is answered live by the `ado_query_work_items`
  WIQL tool (its description says so). **Pull requests are no longer ingested** (AppRiver makes
  merge requests in GitLab; the ADO PR path and its webhook branch were removed).
  **Build pipelines → repositories bridge** (`build_map_document`): one call to
  `build/definitions?includeAllProperties=true` maps every pipeline to the repo + default branch it
  builds, emitted as a doc with `pipeline --builds--> repo` graph edges. This is the **GitLab↔TFS
  link**: MRs land in GitLab, each branch mirrors into a same-named TFS Git repo, builds run in TFS,
  so the TFS repo names match the GitLab repos and the knowledge graph connects a GitLab repo to the
  TFS pipeline that builds it (repo-name aliasing in `deps.py` reconciles spoken forms).
  **Branch-name parity** extends the bridge: every GitLab branch mirrors into the same-named TFS
  branch, so recent build results ingest (via the **Build API**, `build/builds?queryOrder=
  queueTimeDescending` — the Pipelines-runs API omits the branch) as one `builds_document` with each
  build's **source branch** + repo + outcome, and a live **`ado_build_status(branch, repository?)`**
  tool answers "did branch X build?" from a GitLab branch name (4th tool alongside WIQL / code-search
  / get-file). `queueTimeDescending` (not `finishTime`) so never-started builds don't sort to the top.
  Tests in `tests/test_connectors.py` (on-prem URL/search-host/api/verify + SaaS guard;
  `select_recent_iterations` future-exclusion; `build_map_document` builds-edges; `builds_document`
  source-branch; 4-tool name list).
  `octopus.py` **paginates** every list endpoint via `_paged` (follows `Links["Page.Next"]`) — a
  space with >100 projects previously truncated at the `take=100` first page. Pull is a full refresh
  (idempotent via hash dedupe); opt-in `incremental=true` fetches per-project releases only for
  projects with an Octopus event since the last sync (`_changed_project_ids` over `/events?from=`),
  while the paginated project list + the deployment dashboard (which carries the
  `service→deploys→environment` edges) always refresh — falls back to a full pull on first sync or
  any events error. True zero-poll freshness is PUSH via an Octopus Subscription → `/hooks/<source>`.
  Tests in `tests/test_phase4_connectors.py` (paging walks Page.Next, incremental skips unchanged,
  no-watermark full fallback).
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
  Each type also carries `suggests` (seed questions the type contributes to autocomplete —
  `suggest.py` reads them for configured source types); add them when you add a connector.
- `quickjoiner/agent/` — grounded system prompt (`prompts.py`), built-in tools
  (search_memory/remember/list_sources + graph_neighbors/graph_path in `tools.py`),
  **multi-angle + confidence layer (plan 06, 2026-07-17)**: `confidence.py` (pure —
  `classify_evidence(title, uri, kind)` → dependency-map|meeting-notes|authored-doc|generic;
  `score_edge(class, doc_corr, source_corr)` → [0.05, 0.95], weights in one `_BASE` table;
  `score_chain` = min over hops; hop count is deliberately NOT a signal). The `graph_path`
  tool calls `catalog.graph_path_candidates` and, on ≥2 materially different chains, lists all
  of them with per-chain confidence + weak-hop caveats and instructs the model (in tool text)
  to state both or ask ONE clarifying question; single healthy chain output is byte-identical
  to before (golden-tested). `candidates.py` (pure, `parse_triples` strictness): parses the
  optional ```candidates block (semicolon-separated source tags — evidence titles contain
  commas), drops anything whose tags don't resolve to refs actually returned by tools this
  turn, and attaches **server-side** confidence from the per-request **score ledger**
  (`build_agent` threads one dict into `build_builtin_tools` and `OnboardingAgent`; graph
  tools record chain scores per evidence ref; the model's own confidence= number is parsed
  for format and discarded). `agent.py::_finalize` emits the `candidates` SSE event + strips
  the block from the returned prose (history keeps raw text; exception-proof; no-block turns
  are byte-for-byte unchanged).
  **operational tools = the agent-tool bridge** (`ops.py`: scrape_website /
  list_connector_types / add_connector / sync_source — same service functions as the UI
  slash commands; prompt requires explicit user confirmation before add_connector, secrets
  via env: indirection only, scrape only user-given URLs, failed connection tests are not
  saved; wired in `AppContext.build_agent`), tool-call loop (`agent.py`, max 10 rounds —
  **each live tool result is capped to `chat.live_tool_result_max_chars` (default 24000) before
  re-entering the model context**, so an unbounded connector tool like the full Octopus dashboard
  can't overflow the window and make the provider reject the follow-up turn; on hitting the round
  limit the agent makes **one final tool-free turn** so a model that loops on searches still
  answers or properly refuses from what it gathered, instead of a canned "hit the limit" message),
  onboarding briefs (`briefs.py`: seed queries → retrieved chunks → one-shot LLM call → saved to
  `<workspace>/briefs/` and re-ingested; refuses without hits and without building a provider).
  **Repo architecture briefs** (`repo_docs.py`, `qj agents-md <source>` / `POST
  /api/repos/{source}/agents-md`): a per-repo "AGENTS.md from a principal engineer/architect's
  viewpoint," generated from real evidence, never invented — file tree (local clone/`files`
  root, depth-capped), code-graph facts (`defines`/`imports` edges from `ingest/code_graph.py`),
  the repo's dependency-map doc, and retrieved prose (post-filtered to that source_id — `store.search`
  has no source predicate). Strict evidence-tier prompt (manifest > code-graph > prose > layout;
  an absent section states so verbatim rather than inventing architecture). **QuickJoiner-internal
  only**: saved to `<workspace>/generated/<source>/AGENTS.md` + re-ingested (`generated:agents-md`
  bucket) — never written into the repo's own git working tree. If the repo already has a real
  AGENTS.md ingested, this is a **refinement**: the existing doc becomes its own evidence block
  (may carry a stronger model's or a human's judgment) with instructions to preserve/correct/extend
  it rather than overwrite, so a weaker configured model degrades gracefully instead of downgrading
  a good baseline. `provider_override`/`model_override` let one call use a stronger model than the
  workspace default for this specific synthesis. `KnowledgeStore`/`PgVectorStore` gained
  `get_document_chunks(doc_id)` (chunks are the only place original text lives — the catalog only
  stores metadata+hash) and the catalog gained `documents_for_source(source_id)`, both needed to
  read an existing AGENTS.md/dependency-map doc back out. `_graph_facts` resolves the repo entity
  id through `resolve_entity` first, so code-graph facts still surface if entity resolution merged
  the repo's node to a canonical id. **Auto-generate on first sync** (opt-in, `Config.repos.auto_agents_md`,
  OFF by default): `repo_docs.maybe_autogenerate(ctx, source)` fires after a git/files source syncs —
  once (guarded on the generated doc's existence), never on every sync, and never raising (a doc-gen
  failure can't break the sync). Wired into all sync paths (CLI `qj sync`, `POST /api/sync`,
  scheduler, `agent/ops.sync_source`). Exposed in `GET/PATCH /api/settings` under `repos` and the
  Settings drawer's **Repositories** section; per-connector **Architecture brief** button on each
  git/files plate (`POST /api/repos/{source}/agents-md`, opens in `ArtifactModal`). Tests:
  `tests/test_repo_docs.py` (incl. auto-gen fires-once/off-by-default/never-raises), settings
  round-trip in `test_api.py`. NB: `cli.py` now reconfigures stdout/stderr to UTF-8 at startup so
  Rich can't crash rendering a brief/answer containing block/box-drawing/emoji glyphs on a legacy
  cp1252 Windows console (the save+ingest already completed before the render — this stops the
  cosmetic exit-1 crash it caused for `qj agents-md`/`qj brief`).
- `quickjoiner/auth.py` — opt-in local auth. `Auth` over the catalog: PBKDF2 password hashing,
  bearer tokens (sha256-hashed at rest in `auth_tokens`), `users` table. **Open mode until the
  first user exists** (no login, everything shared = pre-auth behavior). Sharing model on
  `SourceConfig` (`owner`, `shared`): ownerless = commons; owned = owner-only unless `shared`.
  `visible()` / `can_manage()` are the gate. Ingested *knowledge* stays one communal memory;
  sharing governs who sees/manages a **connector's config + credentials** and gets its live tools.
  (PLANNED, not built: per-user **knowledge scopes** — query-time union of commons + own +
  shared over the one store, with an ingest-time entity-merge guard and a promotion flow;
  intake 2026-07-18 → `CLOUD_ROADMAP.md` Y1 workstream 8 / PRD W9.3. Must precede multi-user GA.)
- `quickjoiner/api/` — FastAPI. **OpenAPI/Swagger is grouped + documented** (2026-07-20): the app
  carries a top-level `description` + `openapi_tags`, and every route decorator has `tags=[...]` +
  a plain-English `summary=` (the HTML `/` route is `include_in_schema=False`). 43 endpoints across
  11 tag groups (Status / Authentication / Connectors / Sync & ingestion / Ask & search / Knowledge
  graph / Knowledge gaps / Sessions & projects / Briefs & repo docs / Settings / Webhooks). Interactive
  docs at **`/docs`** (Swagger UI — note: pulls its JS/CSS from a CDN, so blank offline; `/openapi.json`
  is self-contained), **`/redoc`**. Import-ready **Postman + Bruno runbooks** for the end-to-end flow
  live in `docs/api/` (all common config in one place: Postman collection Variables / Bruno `Local`
  environment; login captures the bearer token; SSE endpoints noted). Keep tags/summaries current
  when adding an endpoint. (`app.py`: SSE `/api/chat`, sources, sync, search, briefs;
  sessions list/get/distill + `DELETE /api/sessions/{id}` (one) and `DELETE /api/sessions?project=`
  (all, optionally project-scoped) → `catalog.delete_session`/`delete_sessions` (neutral base, both
  backends); `/api/auth/*` status/users/login/logout; `/api/connectors` CRUD + `/test` + `/types` +
  **`/cleanup`** (starts a cleanup job, keeping the config). **`DELETE /api/connectors/{name}`
  purges by default** (2026-07-20): a source_id is `type:name`, so a deleted connector's documents
  are unreachable — nothing can re-sync, refresh or purge them, yet they still answer questions —
  so deletion starts a cleanup job and returns it. `?keep_memory=true` is the explicit opt-out
  (retire the connector, keep what it taught). Both refuse with 409 while a job runs for that
  source, so a purge can never race a live ingest;
  `GET /api/notifications?hours=` (default 24, clamped 1..168) → `SyncManager.recent` — the bell
  menu's feed: running jobs with live state + finished runs from the persisted history, newest
  first, plus an `active` count (read-state is client-side, not stored server-side). Job summaries
  carry `phase`/`percent`; `POST /api/sync/{name}/pause`+`/resume` hold/continue a running job;
  **`POST /api/memory/reset`** wipes ALL ingested knowledge (docs/vectors/FTS/graph/watermarks +
  buckets + gaps) via `catalog.reset_knowledge` + `store.reset`, keeping connectors configured —
  **runs as a background job** (`SyncManager.start_reset`, `kind="reset"`, source sentinel
  `"all memory"`) so it streams logs + lands in the activity feed/history like a cleanup; returns
  `{job}`; refuses 409 while any sync is active (and syncs refuse while a reset runs). The logs SSE
  endpoint was relaxed to serve manager-only jobs (reset, or a just-deleted connector's cleanup) —
  auth-gated, not requiring a configured source;
  `GET/PATCH /api/settings` — the whole `Config` (llm/embedding/retrieval/chat/**graph**) as a
  tunable dict; `GET /api/settings/defaults` — the same groups built from **freshly-constructed
  config models** (never the saved config, or every field would read as default forever), so the
  Settings drawer can mark which fields are still stock without hardcoding the values; `POST /api/llm/test` probes the provider with a one-token round-trip, accepting
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
  Components: `App` (state + SSE orchestration), `Chat` (**full-width answers** — max-w-[1100px],
  no side panel; citations are inline superscripts with a native hover tooltip = the source; a
  per-answer **hover toolbar** — download .md, view **cited-sources modal** (also opened by the
  grounded stamp), 👍/👎; 👎 opens a **feedback modal** that teaches the correction via
  `/api/learn` (`onLearned` refreshes status/gaps); streaming caret), `Rail` (compact fixed-top /
  independently-scrolling conversations / pinned-bottom systems layout with `min-h-0`; per-row
  **delete** on hover + **Clear all** in the Conversations header → `DELETE /api/sessions[/{id}]`,
  clear-all confirmed + project-scoped to what's shown; each **Connected systems** row shows a
  pulsing "syncing…" state and re-opens that job's live log when it has one), `TopBar` (hosts the
  **`NotificationsMenu`** bell before Settings), `Composer`,
  `EmptyState`, `SettingsDrawer` (account + workspace settings + connector plates/forms; the
  workspace pane exposes provider config incl. LiteLLM proxy URL + api-key env var with a
  **Test connection** button hitting `POST /api/llm/test`, and **Retrieval/Knowledge-graph
  toggles** — hybrid, cross-encoder reranker, graph-expansion, contextual chunking (ingest-time),
  and LLM triple extraction — each hinted query-time vs ingest-time; a shared `Toggle` primitive.
  **Every workspace group is a collapsible `Group`** (Language model / Retrieval / Knowledge graph /
  Repositories / Conversations / Embedding), all closed on open, each header badging how many of
  its fields differ from the shipped defaults — counted over the per-section `*_KEYS` lists, which
  name only the fields the drawer actually renders so a badge can never point at a knob with no
  control. Per-field **default markers** (`DefaultNote`, fed by `GET /api/settings/defaults`): a
  neutral "default" when untouched, gold "default: <value>" when changed, so the drawer answers
  both "is this stock?" and "what was it before?". `null` and `""` compare equal (an unset optional
  field is not a customization). The sign-in lock moved from one outer `fieldset` onto each
  `Group`'s body — **reading** settings is never gated, only editing. A **Danger zone** section
  (`DangerZone`) holds the global **Reset all learned memory** action: type-to-confirm (`RESET`) →
  `POST /api/memory/reset`, wording states plainly that connectors/chat/settings are kept),
  **`NotificationsMenu`** (24h activity bell: `GET /api/notifications`, polled by `App` every 3s
  while a sync is active and 10s when idle — so a scheduler- or CLI-started sync still surfaces.
  **Read-state is client-side** in `localStorage.qj_seen_notifications`, keyed `"<job id>:<state>"`
  so one run notifies twice — when it starts and again when it finishes — instead of a completion
  being swallowed by a mid-sync glance. The badge counts unseen; unseen rows carry an accent rail +
  lit background against dimmed viewed rows; closing the menu commits the read-state, so entries
  stay visibly new *while being read*. Rows with `live` re-open the log viewer),
  `api.ts`
  (typed client + SSE reader), `ui.tsx` primitives. Build: `npm run build` → `frontend/dist`;
  dev: `npm run dev` proxies /api+/hooks to :8787. FastAPI serves the UI per `_ui_dir()`:
  `QJ_UI_DIR` env → repo `frontend/dist` → legacy `api/static/index.html` fallback (kept for
  wheel installs without the built frontend). Docker builds the UI in a node:22 stage and sets
  `QJ_UI_DIR=/app/ui`.
- `quickjoiner/export.py` — markdown → md/html/csv/pptx (`--format` on `qj ask` / `qj brief`);
  SSE chat events: `thinking` / `delta` / `tool_call` / `candidates` / `answer` / `error` / `done`.
- `quickjoiner/sessions.py` — `SessionManager`: persistent sessions + projects (catalog tables
  `projects` / `chat_sessions`, messages stored as JSON snapshots). Token optimization: history
  over `chat.compress_after_est_tokens` is folded into a rolling summary at a **user-turn
  boundary walking backward** (never splits assistant+tool groups, never shrinks the recent
  window); LLM summarizer with deterministic digest fallback (works keyless); persisted tool
  outputs truncated to `chat.tool_result_max_chars`. Compression/`distill` extract durable
  facts → ingested as `conversation://<project>/<session>` docs (source `conversations:learned`)
  so past conversations are searchable memory. Project name/description + rolling summary are
  injected via `build_agent(extra_system=...)`.
- `quickjoiner/suggest.py` — **question autocomplete** (`QuestionSuggester`, `GET /api/suggest?q=&limit=`,
  composer typeahead). **Keyless + deterministic** (no LLM per keystroke — must be instant and the
  broker is flaky), computed **live** from the workspace so it self-improves with every connected
  system: (1) **entity-templated** questions — `extract_needle_tokens` strips question/filler words to
  find the noun the user is naming, `catalog.search_entities` resolves it against the knowledge graph,
  and per-type `TEMPLATES` fill natural questions ("Where is {service} deployed?", "What does {repo}
  depend on?"); (2) **past questions** from the gaps backlog (`catalog.list_gaps`); (3) **source-aware
  starters** pulled from the connector catalog (`connectors/specs.py` → `FORM_SPECS[type]["suggests"]`),
  so a new connector ships its own openers. `rank_suggestions` (pure, banded: history-prefix > starter-
  prefix > entity-template > substring) dedupes/caps; `fill_templates` boosts a template whose intent
  verb aligns with what's typed ("dep" → the depend question), prefix-aware so half-typed words match.
  **Entity re-ranking by match quality** (`entity_name_match`): `search_entities` orders purely by graph
  degree, so a very-connected entity that only matched via an *alias* (its name lacks the typed word) can
  bury real name matches — the suggester rescores, preferring name hits (exact word 3 / word-prefix 2 /
  substring 1 / alias-only 0), then #needle-tokens matched, then degree; so "manage" → the *Management*
  services (not high-degree alias-only "Black Team"), "securetide mxchecker" → `AppRiver.SecureTide.MXChecker`.
  **Pipeline integration = the ingest pipeline populates the graph** (deps.py maps, code_graph edges,
  ticket/entity extraction in `_sync_graph`) on every sync, and the suggester reads that graph live —
  so connecting+syncing any new source automatically enriches autocomplete with that system's real
  entities, no rebuild. Tests: `tests/test_suggest.py` (needle/template/rank pure + a FakeCatalog
  suggester), `/api/suggest` round-trip in `test_api.py`. Frontend: `api.suggest`, debounced typeahead
  in `components/Composer.tsx` (↑/↓ navigate, Tab/Enter-on-highlight accept, Esc dismiss, skips
  slash-commands).
- `quickjoiner/evals/harness.py` — YAML eval sets; deterministic retrieval layer (no LLM) +
  agent layer (refusal phrasing via `REFUSAL_MARKERS`, citations, keywords). Reports saved to
  `<workspace>/evals/*.json` for before/after comparison when tuning threshold/embedding/chunking.
  **Threshold calibration** (`calibrate(results, floor=0.90)`, `qj eval SET --calibrate [--apply]`):
  sweeps `min_score` t ∈ [0.30, 0.80] and reports the value that best separates grounded answers
  from refusals — maximizes `(grounded_recall + refusal_accuracy)/2` subject to `refusal_accuracy
  ≥ floor`, then picks the **midpoint of the optimal band** (maximum margin), NOT an edge, so it
  never sits one document away from false-refusing real content. `RetrievalCaseResult.hit_score`
  (the expected hit's cosine) is the value swept. Flags **thin** sets (< `CALIBRATION_MIN_CASES`
  answerable or refusal cases) as untrustworthy; `--apply` writes the threshold via
  `catalog.save_config` (opt-in — default only prints). **Report comparison** (`compare_reports`,
  `qj eval SET --compare old.json`): per-metric delta table over `COMPARE_METRICS`
  (`false_refusal_rate` is lower-is-better), **exits non-zero** if any watched metric regressed by
  more than `COMPARE_TOLERANCE` (0.02) — the CI merge gate for any retrieval change.
- `quickjoiner/scheduler.py` — APScheduler periodic syncs for sources with `sync_interval_minutes`.
- `quickjoiner/sync_manager.py` — **startable / stoppable / live-logged sync jobs** (`SyncManager`).
  Each sync runs on its own daemon thread, so **multiple different sources sync concurrently**
  (a second job for the *same* source is refused). Interruption without touching `pipeline.ingest`:
  the job wraps the connector's document stream in a generator that checks a `threading.Event` before
  each document, so a stop halts cleanly between docs (partial, already-committed work is idempotent).
  Progress lines append to the job and fan out to SSE subscribers (`subscribe` replays the backlog so a
  late viewer sees the whole run). **Graph-safe cleanup** — a `clean` start (or a stop-with-cleanup)
  purges first/after via new catalog primitives: `delete_documents_for_source` (deletes docs +
  cascades their graph **edges**), `store.delete_source` (vectors + FTS), `gc_orphan_entities` (drops
  dangling graph nodes + aliases, keeping the invariant that every node has ≥1 cited edge), and
  `clear_sync_state` (next sync = full pull) — so docs, vectors, FTS and the knowledge graph stay
  mutually consistent (a half-synced or stale-shape source can't leave a corrupted graph). API:
  `POST /api/sync/{name}[?clean=true]` **now starts a job** (was synchronous) → `{job}`;
  `POST /api/sync/{name}/stop[?cleanup=true]`; `POST /api/sync/{name}/pause` + `/resume`;
  `GET /api/syncs`; `GET /api/sync/{name}/logs` (SSE).
  **Pause/resume + bounded stop latency + stages/% (`sync_control.py`, 2026-07-20):** Python
  can't kill a worker thread, so cancellation is cooperative — a stop is only seen where code
  *checks*. Before, that was only between the documents a connector yields, so a connector deep in
  a non-yielding phase (TFS makes ~11 HTTP calls per team before its first yield; then the
  graph-drain waits on whole batches of broker calls) could run **10+ minutes** past a stop. Fix: a
  per-job **`SyncControl`** (built in `_build_control`, wired to the job's `cancel`/`gate` events) is
  threaded everywhere — `_tracked` (between docs), the connectors' inner loops via
  `Connector._checkpoint()` (each job gets a fresh connector instance, so `connector._control` is
  thread-safe; no `sync()` signature change), and the pipeline's triple-drain (one wave per
  `triple_workers`, checkpoint between waves ⇒ at most that many broker calls in flight at a stop).
  `control.check()` **raises `SyncStopped`** (caught in `_run` → clean `stopped`) and **blocks while
  paused**; `control.stage(name, done, total)` reports the phase + optional progress. Target
  latency ≤20s; the one thing it can't interrupt is a single external call already in flight
  (bounded by that call's timeout). **Pause** (`pause`/`resume`; `SyncJob.gate` — set=go, clear=hold)
  holds both the pull and the drain; the same in-memory run continues on resume (no re-pull); stop
  sets the gate so a paused worker wakes to cancel. **Stages + estimated %**: connectors call
  `_stage()`. **Accurate %** where a total is knowable up front: `files`/`git` (scanning → reading
  N/total → dependency map), `azure_devops` (phase + per-team `work items · Team A`, i/teams), `jira`
  (from the search API's `total`), `octopus` (per-project), `confluence` (per space, via a one-call
  CQL `totalSize` preflight — `_space_page_count`; unscoped all-spaces pull ⇒ no total ⇒ shimmer).
  **Phase label only** (UI shows a live shimmer, no fake denominator) where the API gives no cheap
  count: `github`/`gitlab` list APIs. All of these also `_checkpoint()` between page/section fetches so a stop lands
  within a page. The remaining connectors (logsearch inventory, web_scrape) rely on the universal
  between-document check — they finish in seconds / yield per page, so no long non-yielding stretch
  to instrument. `SyncJob` gains `phase`/`phase_done`/`phase_total` + `percent()` (surfaced in every
  `summary()`, so `/api/syncs` + `/api/notifications` carry them).
  **Cleanup jobs** (`start_cleanup(name, source_id)`, `SyncJob.kind` = `sync|cleanup`): the same
  `_purge` as a clean sync, without the re-pull — documents (cascading graph edges), vectors, FTS,
  orphan graph nodes and the watermark. It takes `source_id` as an argument rather than looking the
  config up, because the case that matters most is a source whose config is already gone. The
  `sources` row is deliberately left alone: a still-configured connector must stay listed with zero
  documents (like one added but not yet synced), and for a deleted connector `save_config` has
  already reconciled the row away. Not stoppable — a half-purge is the inconsistency it prevents.
  **24h activity history** (`recent(hours)`, `GET /api/notifications`): the job map is in-memory
  and per-process — enough to *watch* a run, not to remember one — so each run is also persisted
  to the catalog's `sync_events` table (`record_sync_event` on start, upserted on the same id when
  it ends; `list_sync_events`/`prune_sync_events`, neutral `_SqlCatalog` ⇒ both backends,
  7-day retention). `recent()` overlays live job state on that history and marks each row `live`;
  a row left `running` by a process that died is reported as **`interrupted`**, never as a sync
  that is still going. History reads are best-effort — a catalog that can't serve them degrades to
  live jobs rather than failing the request. Job ids carry a uuid suffix (`sync-3-4ebb86c1`) so a
  restarted process can't reuse an id the history already holds.
  CLI `qj sync [--clean]` / `qj resync <name>` and the `sync_source(name, clean=)` agent tool run
  the same purge synchronously. Web UI: each connector plate has **Sync now** + **Clean re-sync** +
  **Clean up** (eraser — two-click armed confirm; forgets the source's documents/vectors/graph
  edges but keeps it configured), all opening `SyncLogModal` (live log stream, a **Stop** button
  that asks *"clean up partial data?"* — not offered for cleanup jobs). Deleting a connector (trash,
  also two-click) runs that same cleanup automatically and opens its log. `EditConnectorModal`
  shows **Name disabled with the reason** — identity: `source_id` = `type:name`, so every doc id,
  vector, graph node, watermark and webhook URL derives from it, and renaming would orphan the lot.
  **The log panel is a viewer, not a leash** — it closes at any time (X / backdrop / Esc / "Run in
  background") and the job keeps running; re-opening re-attaches, and since `subscribe` replays the
  backlog a reattached viewer sees the whole run, including one started before a page reload. Its
  `autoStart` prop separates the two entries (press Sync = start then watch; click a running job =
  attach only). The viewer is owned by `App`, not the settings drawer, so a running sync stays
  reachable with the drawer closed — from the bell menu or the rail's Connected-systems row. The
  modal shows a **Pause/Resume** button + a **stage/% progress bar** (determinate when a total is
  known, an indeterminate shimmer for open-ended pulls), polling `/api/syncs` every 2s while active
  to keep the % fresh; the rail/plate pills and bell rows show the live % and a "paused" state.
  **`SyncHistoryModal`** (bell menu → "View full history by connector") groups the 7-day
  `/api/notifications` window by connector, newest-first, each group's latest/ongoing run shown with
  its live stage + %, earlier runs collapsed.
  Tests: `tests/test_sync_manager.py` (interruption, clean-start purge, double-start conflict,
  stop-with-cleanup, backlog replay, history record/restart-interrupted/degrade, id uniqueness,
  **pause/resume, stop-while-paused, sub-2s stop inside a non-yielding connector loop, graph-tail
  pause**), `tests/test_catalog.py` (sync_events upsert/window/prune) + async-sync/clean-resync,
  pause/resume, and `/api/notifications` round-trips in `test_api.py`.

## Conventions & gotchas

- **Grounding threshold**: `retrieval.min_score = 0.55`, empirically tuned for bge-small
  (relevant ≥ 0.64, unrelated ≤ 0.55). Retune if the embedding model changes **or if
  `embedding.instruct` is toggled** (asymmetric query instructions shift the cosine distribution).
  Borderline hits are passed to the LLM with scores; the prompt makes the final relevance judgment.
  Don't guess the retune by hand — `qj eval SET --calibrate` recommends the value from a real eval
  set (and `--apply` writes it). It picks the **midpoint of the optimal band**, so it won't jump to
  a false-refusal-heavy threshold, and it warns when the eval set is too thin to trust.
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
- **Keep the API surface fully documented — always.** Any change to an HTTP endpoint (adding,
  removing, renaming, or changing the method/path/params/request body/response shape/status codes
  of a route in `quickjoiner/api/`) must, **in the same change**, keep all of the following in sync
  — treat any of them drifting as a broken build:
  1. **OpenAPI/Swagger** — every route carries an accurate `tags=[...]` (one of the existing
     `_OPENAPI_TAGS` groups; add a group there if a genuinely new area appears) and a plain-English
     `summary=`; a docstring serves as the longer description. A new endpoint is untagged/unsummarized
     = incomplete. `/docs` is generated from these, so this IS the API documentation.
  2. **The runbooks in `docs/api/`** — the **Postman** collection (`QuickJoiner.postman_collection.json`)
     AND the **Bruno** collection (`docs/api/bruno/`): add/rename/remove the request in the right flow
     folder, keep bodies/params matching the real request models, and put any new tunable value in the
     **one** variables place (Postman collection Variables / Bruno `environments/Local.bru`) — never
     hardcode it in a request. Update `docs/api/README.md` if the flow or variable set changes.
  3. The endpoint's user-facing mention in `README.md` / its architecture bullet in `CLAUDE.md`
     (per the rule above).
  Verify the schema still builds (`app.openapi()`), and that request bodies/params in the runbooks
  match the models — a runbook that 4xxs against the real API is worse than none.
- **Plans and roadmaps are forward-looking; graduate finished work OUT of them.** `docs/plans/`
  and the roadmaps embedded in the strategy docs describe work that is **not yet done**. The
  moment a slice ships (or is deliberately dropped), in the **same change**:
  1. **Graduate the substance** — fold *how it actually works* into the descriptive source of
     truth: the relevant `CLAUDE.md` architecture bullet AND the matching `docs/` architecture/
     strategy section. "What exists" is described there, never only in a plan.
  2. **Remove it from the forward lists** — delete the slice from the plan's outstanding items,
     and delete the completed item from any **roadmap's pending list** (`AI_ROADMAP.md`
     tier/S/X lists, `PRD.md` W-wishlist, `FRONTEND_ROADMAP.md` F-items, `CLOUD_ROADMAP.md`
     Y-items, `MARKET_ASSESSMENT.md` Appendix A). A done item is *moved out*, not left inline marked
     "shipped" — record it, if worth it, in that doc's short **Shipped** ledger with a pointer to
     where it's now documented. Keep any cross-reference anchors intact (leave a one-line stub if
     a number is referenced elsewhere).
  3. **Update the trackers** — `docs/plans/STATUS.md` (master table + outstanding list), the
     Status column in `docs/plans/README.md`, **and `docs/PRIORITIES.md`** (the one ordered
     cross-roadmap backlog): a shipped/dropped item's row is deleted, a re-scoped item's
     value/effort re-scored, a new item ranked in.
  4. **Delete emptied plans** — when a plan has **nothing outstanding left**, delete the plan file
     entirely (git preserves it) and record it in STATUS.md's **"Shipped & removed"** ledger with
     a pointer to where its substance now lives. Do NOT keep a ✅ tombstone plan around.

  The invariant this buys: an open plan or a pending roadmap item **always** means genuinely
  unbuilt work — never a finished thing masquerading as a backlog — and the architecture/strategy
  docs are the single description of what exists. Reconcile against the tree, not from memory: a
  feature counts as shipped only when its code/tests actually exist.
- **Keep the strategy & design docs live — always.** The docs under `docs/` are **living
  documents that must give a true snapshot of the repo at all times**, not write-once artifacts —
  they are the destination the rule above graduates finished work INTO. Any change that shifts
  architecture, capabilities, roadmap position, test posture, or the product story must update the
  affected doc **in the same change**: `docs/AI_ARCHITECTURE.md` (invariants I1–I3, stack-as-built,
  limitations — descriptive only), `docs/AI_ROADMAP.md` (ALL pending AI work: quality tiers,
  speed/cost S-track, research X-track, frontier process), `docs/PRD.md` (story/AC/test status + the W-wishlist),
  `docs/TEST_STRATEGY.md` (coverage program + T1–T4 — reflect new suites, gaps closed, and honest
  remaining holes), `docs/FRONTEND_ROADMAP.md` (F0–F2), `docs/CLOUD_ROADMAP.md` (Y1–Y5),
  `docs/MARKET_ASSESSMENT.md` (Appendix A connector matrix + differentiators), `docs/PITCH_DECK.md`
  (claims must match what actually ships — never let the deck outrun the code), `docs/KNOWLEDGE_GRAPH.md`,
  `docs/design/DESIGN_VISION.md`, and **`docs/PRIORITIES.md`** — the single ROI-ordered
  backlog across ALL roadmaps/plans (rank + value/effort + links only, no design content).
  **Any change to any roadmap or plan — item added, removed, shipped, or re-scoped — updates
  `docs/PRIORITIES.md` in the same change**; a row there must always correspond to a live
  item in its source doc. A claim that's no longer true is corrected, not left to rot.
  Reconcile against the tree, not from memory. Treat any of these drifting out of sync with the
  code as a broken build, exactly like `CLAUDE.md`/`README.md`.

## Strategy & design docs (2026-07-11, "champion team" review)

`docs/PRD.md` (stories+ACs+tests, wishlist W1–W10), `docs/MARKET_ASSESSMENT.md` (honest:
not unique as "chat over docs"; differentiators = honesty contract, evidence graph,
local-first, ramp metrics), `docs/PITCH_DECK.md`, `docs/AI_ARCHITECTURE.md` (descriptive:
invariants I1–I3, stack-as-built, limitations, model strategy), **`docs/AI_ROADMAP.md`**
(2026-07-17 — the single forward-looking AI doc: quality tiers #2/#5–#22, speed/cost
S-track S1–S5, original-research X-track X1–X7 with pre-registered spikes, and the standing
frontier process — per-change eval/bench gates, monthly frontier scan, quarterly research
spike, each with a ready-to-paste prompt), `docs/FRONTEND_ROADMAP.md` (F0–F2),
`docs/TEST_STRATEGY.md` (>90% program, T1–T4), `docs/CLOUD_ROADMAP.md` (Y1–Y5),
`docs/design/DESIGN_VISION.md` + `orrery-prototype.html` (fog-of-war "Orrery" concept,
published as a Claude artifact). These are the authoritative roadmap references.
**`docs/PRIORITIES.md` (2026-07-18)** is the single ROI-ordered backlog ACROSS all of them
(value/effort matrix + links only; detail stays in the source docs) — kept in lockstep with
every roadmap/plan change per the house rule above.
**Execution plans** (each with a ready-to-paste prompt): `docs/plans/` — see `docs/plans/STATUS.md`
for the live tracker. Live plans: 03 Slack+Teams connectors, 04 coverage fog, **05 evaluate
retrieval & correlation on a connected org** (the "decide with data" runbook for the a–e stack +
embedding-change decision; run on an org-connected machine), 06 multi-angle confidence (code
shipped, verification open), 07 multimodal derive-to-text (vision/audio/video via specialist
models + deterministic extractors; roadmap #23; not started). Shipped & removed (graduated into these docs): 01 knowledge-debt
backlog, 02 retrieval quality pack. Finished plans are deleted, not kept — per the house rules
above, only unbuilt work lives under `docs/plans/`.

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
  also render ```mermaid fences via the shared `<Markdown>` component (`components/markdown.tsx`;
  see the markdown-renderer status note below). After a scrape the UI asks
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
- Knowledge-debt backlog (2026-07-13, PRD W2.1–W2.3; plan shipped + removed — see
  `docs/plans/STATUS.md` ledger): **every refusal is a
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
- Markdown renderer — GFM gap-closing + shared `<Markdown>` component (2026-07-15, user request):
  kept the hand-rolled, **dependency-free** renderer (a react-markdown + remark-gfm swap was tried
  and reverted — it caused a UI regression in the live browser; the handwritten path renders
  cleanly and is what ships). Widened `components/markdown.tsx` to a broad GFM subset so it reads
  close to remark-gfm without the bundle/runtime cost: **GFM pipe tables**, **blockquotes**
  (nested, recursive), **nested lists** (indentation-based, re-parsed as child blocks) + **task
  lists** (`- [ ]`/`- [x]` → real checkboxes), **strikethrough**, real **`[text](url)` links** +
  **bare-URL autolinks** (disambiguated from citations — the link alternative precedes the bare-
  `[ref]` alternative in the inline regex, so `[x](y)` is a link and `[x]` a citation), **images**,
  `# … ######` headings, **`---` rules**, and **two citation forms** — bare `[ref]` and
  `【source: …】` lenticular (`source:` label stripped) — because **every model cites differently
  and none obeys the prompt's `[uri]` format**: gemma text, gpt-oss uses `【source: Title】`. Both
  become gold citation superscripts + entries in the sources modal; retune `INLINE_SRC` if a model
  invents a new form. (`<url>` angle-bracket autolinks were a third citation form until
  2026-07-15 — see the fix below; they're now plain links, not citations.)
  One big named-group inline regex (built fresh per
  `renderInline` call because it recurses into emphasis bodies — a shared global's lastIndex would
  be clobbered). Extracted a shared **`<Markdown text book? mermaid? className?>`** component
  (bottom of `markdown.tsx` — same file, since Windows is case-insensitive and a separate
  `Markdown.tsx` would collide with `markdown.tsx`) as the org-wide entry point; `ArtifactModal`
  uses it, `Chat` still calls `renderMarkdown(text, book)` directly because it needs `book.refs`
  for the provenance ledger. `CiteBook`/`renderMarkdown`/`renderInline` remain exported.
  Verified by SSR-rendering the real engine to HTML + a Playwright screenshot of every feature;
  typecheck + build green; frontend rebuilt.
- Clickable citations + `<url>` over-citation fix (2026-07-15, user-reported bug + request): citation
  superscripts (`[ref]` / `【…】`) are now wrapped in a link (opens in a new tab) when the ref itself
  is a URL — hover still shows the source via the native `title` tooltip either way, unchanged when
  the ref is a title/label (no href to point at). Root cause found live: `search_memory` returns raw
  chunk text verbatim, and when a source document itself contains links (e.g. a Confluence page
  listing TFS/PR links), the model relays them with GFM `<url>` autolink syntax — legitimate markdown,
  not a citation gesture — but `<url>` had been folded into the same citation heuristic as `[ref]`/
  `【…】`, so every content link inside the one retrieved document inflated the "Grounded · N sources"
  count and cluttered the sources modal (verified: a Confluence-only answer showed 10 "sources", one
  per TFS/GitLab link mentioned on that single ingested page). Fix: `<url>` (`angleUrl` capture group,
  `components/markdown.tsx`) now renders as a plain clickable link, same as a bare autolink — it no
  longer calls `book.number()`. Only bare `[ref]` and `【…】` remain real citation forms. Verified live
  against a captured production answer (session in `~/.quickjoiner/default`) via Playwright: badge
  count dropped from inflated-by-content-links to the true `1 source`, TFS/GitLab links render as
  normal teal links. Frontend rebuilt; no dedicated frontend test suite exists yet (manual/Playwright
  verification is the current practice, per the markdown-renderer entry above).
- Question autocomplete (2026-07-16, user request): `quickjoiner/suggest.py` +
  `GET /api/suggest` + a debounced typeahead in the composer help a user finish a question fast.
  **Keyless/deterministic** (no LLM per keystroke) and computed **live from the knowledge graph**,
  so it self-improves with every connected system — the user's explicit requirement that "this
  update process be part of the pipeline" is met by the ingest pipeline populating the graph on
  every sync (deps maps, code-graph edges, ticket/entity extraction) and the suggester reading it
  live; connecting+syncing a new source immediately surfaces that system's real services/repos/
  environments as templated questions, no rebuild. Per-connector-type openers live in the connector
  catalog (`FORM_SPECS[type]["suggests"]`) so a new connector ships its own starters. Verified live
  on the Appriver workspace: "what env" → "What environments are configured in Octopus?"; "connector"
  → repo/dependency questions; "production" → deployment questions. Tests: `test_suggest.py` +
  `/api/suggest` in `test_api.py`; frontend rebuilt.
- Live agent robustness against gpt-oss + a flaky broker (2026-07-16, found while live-testing the
  Octopus connector's real scenarios on the Appriver workspace, provider `litellm`/`gpt-oss-120b`):
  three real bugs blocked every tool-using answer, now fixed with regression tests.
  **(1) Tool names with spaces → HTTP 500.** Connector live-tool names embed the source name
  (`octopus_deployment_status_Appriver Octopus`); the space breaks gpt-oss's Harmony tool-call wire
  format and the broker returns `500 "unexpected tokens remaining in message header:
  to=functions.octopus_deployment_status_Appriver"`. Fixed by sanitizing at `ToolSpec.__post_init__`
  (`base.sanitize_tool_name`, OpenAI's `^[a-zA-Z0-9_-]{1,64}$`) — the single choke point for every
  tool. This was the "tool use failure" seen the prior night. **(2) Unbounded tool output →
  context overflow → HTTP 400.** The `octopus_deployment_status` live tool dumps the whole dashboard
  (one line per project×environment — ~531 KB for 519 projects), pushing the prompt past the model's
  window; the proxy then computes a *negative* `max_tokens` and rejects with `400 "max_tokens must be
  at least 1, got -86016"`. Fixed by capping each live tool result to `chat.live_tool_result_max_chars`
  (default 24000) in the agent loop before it re-enters context. **(3) Flaky broker → raw traceback.**
  The internal model broker intermittently returns 401/500 for a *valid* static key under load (same
  key alternates 200/401 within seconds); the LiteLLM provider now retries `{401,429,5xx}` + network
  errors with bounded exponential backoff. Also: on the 10-round tool-call limit the agent makes one
  final tool-free turn (a model that loops search→confluence→scrape now still answers/refuses instead
  of a canned "hit the limit" message). Live-verified scenarios once the broker cooperated: "what
  environments exist in Octopus?" → 10 environments cited; "which envs is appriver-management-console
  deployed to + prod version?" → per-env table (DevLab 0.1.16 / Production 0.1.14) cited. Suite:
  **299 passed, 10 skipped** (pre-existing unrelated `test_evals.py` multi-hop-yaml failure remains).
- Plan 06 — multi-angle, confidence-scored answers (2026-07-17, all four phases A→D→B→C):
  `graph_path_candidates` + ambiguity in the `graph_path` tool text (materially different chains
  all surfaced, clarifying-question prompt guidance), entity-resolution adjudicator now judges
  from evidence context (5-arg seam, `entity_evidence`), deterministic `score_edge`/`score_chain`
  confidence wired into the graph tools (per-chain scores, weak-hop caveats, meeting-notes flags,
  `edge_corroboration`), and the `candidates` block → SSE event → `CandidateCarousel` UI (strict
  parse, server-side score ledger — LLM's self-reported confidence discarded). See the plan doc
  (`docs/plans/06-…md`, hardened + §6 implementation plan) for the full design. Suite: **378
  passed** (+54 over the pre-plan baseline), 10 skipped, pre-existing eval-yaml failure unchanged.
  §2.7's live check (`resolve_entity('webroot connector')` merging after re-sync) is pending the
  user's next clean re-sync of Connector+Confluence — the merge only fires at ingest time.
- Plan 02 completed — retrieval quality pack parts B + C (2026-07-17). **B. Threshold calibration**
  (`evals/harness.calibrate` + `qj eval --calibrate/--apply/--compare`): sweeps `min_score`
  over an eval set and recommends the **max-margin midpoint** of the optimal band (not an edge —
  a refinement over the plan's tie-break-toward-higher, which over-jumped to ~0.74 on a thin
  2-doc set; the midpoint gives ~0.55 there and warns the set is thin), subject to a
  refusal-accuracy floor; `--apply` persists it, `--compare` is a non-zero-exit CI regression gate
  over the summary metrics. **C. Alias query expansion** (`memory/expansion.py` `expand_query`,
  `retrieval.alias_expansion`): appends canonical entity names for org spoken-forms in the query
  (1–4-token windows, reuses `catalog.resolve_entity`, `_GENERIC_TOKENS` stopword skip, cap 3),
  wired into `search_memory` + `/api/search`, best-effort. Suite: **399 passed**, 10 skipped,
  the pre-existing missing-`docs/evals/` eval-yaml failure unchanged. Live-verified on the AppRiver
  graph (`connector monitor` → `AppRiver.Connector.Monitor`) and via a scratch calibrate/apply/
  compare run. (Part A, contextual chunking, shipped earlier on 2026-07-13.) Plan 02 is fully
  shipped; its plan file has been removed and its substance graduated here + into the
  `AI_ROADMAP.md` Shipped ledger (see the `docs/plans/STATUS.md` "Shipped & removed" ledger).
- Prompt caching + stable-prefix discipline (2026-07-18, the S5 fast-half): Anthropic
  `cache_control` breakpoints in `anthropic_provider._build_kwargs`/`_mark_cache_breakpoints`
  (system block caches tools+system; moving message breakpoint + ~15-block intermediate
  markers inside the API's 20-block lookback; `llm.prompt_cache` config gate, ON) and
  deterministic name-sorted tool specs in `OnboardingAgent` (byte-stable prefix for Anthropic
  explicit caching, OpenAI-compatible automatic prefix caching, and Ollama/llama.cpp KV
  reuse). Bonus fix in the same file: Anthropic thinking now sends `{"type": "adaptive"}` —
  the old `budget_tokens` form 400s on the default model Opus 4.8. Details in the `llm/`
  architecture bullet; tests `tests/test_prompt_cache.py` (12: breakpoint placement/spacing/
  cap, thinking-block skip + no-history-mutation, cache-off byte-identical path, sorted
  specs). Suite: **426 passed**, 10 skipped, pre-existing eval-yaml failure unchanged. Live
  cache-hit verification (`usage.cache_read_input_tokens` > 0, logged at DEBUG) pending an
  `ANTHROPIC_API_KEY` — none on this machine.
- LiteLLM / gpt-oss enhancement pack (2026-07-18, same session): Harmony reasoning round-trip
  (agent stores `reasoning` on tool-call turns → `reasoning_content` on the wire → stripped on
  session persist), `llm.reasoning_effort` + `llm.parallel_tool_calls` + `llm.proxy_cache_control`
  config knobs, `stream_options.include_usage` + `cached_tokens` DEBUG logging (the LiteLLM-side
  cache-hit verification), strip-and-retry on 400s naming an optional param or an impossible
  `max_tokens`, Retry-After-aware backoff, and a pooled `httpx.Client`. Full detail in the
  `llm/` bullet above. New config fields round-trip through `GET/PATCH /api/settings`
  automatically but have no Settings-drawer controls yet (frontend not rebuilt). Suite:
  **438 passed**, 10 skipped, pre-existing eval-yaml failure unchanged. Live verification on
  the AppRiver broker (does gpt-oss loop less with reasoning round-trip? does `cached_tokens`
  move?) pending the user's next live session with DEBUG logging.

- Non-blocking sync viewer + 24h activity menu (2026-07-20, user-reported): the sync log modal
  hid its close affordance while a job ran (so watching a sync held the UI hostage) and nothing in
  the app read `/api/syncs`, so a page reload erased every trace of a running sync even though the
  server was still happily syncing. Both were frontend-side blindness, not backend gaps. Now: the
  log panel closes freely and re-attaches on demand (`autoStart` prop; viewer lifted from
  `SettingsDrawer` to `App`), running syncs are indicated on the rail row + connector plate, and a
  **`NotificationsMenu`** bell in the `TopBar` shows the last 24h of sync activity with
  unseen/viewed differentiation. Backed by a new persisted `sync_events` history
  (`GET /api/notifications`) so the feed outlives a server restart, not just a reload. Suite:
  **445 passed** (+7), 11 skipped, pre-existing eval-yaml failure unchanged. Verified live in
  Chrome via Playwright against a scratch workspace: close-while-running, reload-restores-state,
  badge/unread transitions, re-open-from-menu, and history surviving a restart.
- Settings drawer: collapsible groups + default markers (2026-07-20, user request): all six
  workspace groups now collapse like Embedding did (closed by default — the pane was a wall of
  knobs), each header badges its changed-field count, and every field shows whether it still holds
  the shipped default. Defaults come from a new `GET /api/settings/defaults` built from fresh
  config models rather than a hardcoded frontend copy, so they can't drift from `config.py`.
  `Field`/`Toggle` gained an optional `note` slot; `Field` is now a full-height flex column so a
  wrapped label can't misalign the inputs in a two-column row. Suite: **446 passed** (+1: the
  defaults endpoint is asserted to move independently of saved settings). Verified in Chrome:
  all-collapsed on open, 6 default markers in Retrieval, marker → "default: 0.55" + a
  "1 changed" header badge after an edit, badge still visible when collapsed.

- Connector cleanup + orphan-proof delete (2026-07-20, user request after asking what renaming a
  connector would do): a connector's **name is its identity** — `source_id` is `type:name`, which
  keys doc ids (`sha256(source_id|uri)`), chunk/FTS rows, the `repo:<name>` graph node, the sync
  watermark, the `/hooks/<name>` URL, live tool names, and (via contextual chunking) the text that
  gets embedded. Renaming was never wired up (no `name` on `ConnectorUpdate`, no field in the edit
  modal); it is now **shown disabled with the reason** instead of silently absent. The related hole
  — deleting a connector left its documents in memory permanently unreachable (nothing could
  re-sync or purge a source_id with no config) — is closed: **delete purges by default** via a
  background cleanup job (`keep_memory=true` opts out), and a standalone **Clean up** action
  forgets a source's knowledge while keeping it configured. Cleanups reuse the whole sync-job
  surface: live log panel, rail/plate indicators, and the 24h activity feed (`kind` column on
  `sync_events`, "cleaned up" labels). Caught in live verification: the cleanup job was also
  dropping the catalog `sources` row, making a still-configured connector vanish from Connected
  systems — now left alone, with an API test pinning it. Suite: **453 passed** (+7), 11 skipped,
  pre-existing eval-yaml failure unchanged. Verified in Chrome + over the API: cleanup log
  ("removed 20 documents, 4 orphan graph nodes"), memory → 0, connector still listed, re-syncable;
  delete → cleanup job → 0 docs / 0 sources / 0 graph nodes.

- Sync pause/resume, bounded stop latency, stages + % , history-by-connector (2026-07-20, user
  request; the stop latency was a reported bug — a TFS sync ran 10+ min past a stop). New
  `sync_control.py` (`SyncControl`/`SyncStopped`) threads one cooperative control through the
  connector inner loops, the document loop, and the graph-drain, so stop/pause is honored within
  seconds (target ≤20s; live-measured 0.73s on a files sync, <2s unit-tested inside a non-yielding
  loop) instead of after the whole phase. Pause holds both the pull and the deferred triple-drain;
  resume continues the same in-memory run. Connectors report stages + progress via `_stage()`
  (files/git/jira/ADO/octopus/confluence = accurate %; github/gitlab = phase label + shimmer; the
  low-volume logsearch/scrape connectors need no instrumentation — the universal between-doc check
  already bounds their stop latency). Instrumented connectors also `_checkpoint()` between
  page/section fetches. UI: Pause/Resume button + stage/% progress bar in the log modal, live
  % on the rail/plate pills and bell rows, and a new `SyncHistoryModal` (7-day history grouped by
  connector, latest run's live %). Suite: **466 passed**, 11 skipped, pre-existing eval-yaml failure
  unchanged; connector-level tests assert jira/gitlab/confluence actually honor a cancelling control
  + report the right stages (confluence: accurate % scoped, shimmer unscoped). Verified live in Chrome: stage
  progression (scanning → reading N/total → dependency map), climbing %, 0.73s stop, and the
  history view. **Caveat carried from the pause work:** none of this can pause/stop a sync already
  running in an OLD server process — it's for syncs started after the restart.

- Global "reset all memory" (2026-07-20, user request — "clean state as if nothing synced, keep
  connectors configured"). Per-connector cleanup existed but missed the non-connector buckets
  (taught notes, distilled conversations, webhook pushes) and any orphaned sources. New
  `catalog.reset_knowledge()` + `store.reset()` (+ `PgVectorStore.reset()`) wipe every document,
  vector, FTS row, the whole knowledge graph, all sync watermarks, the ingestion-bucket source rows
  and the gaps backlog — keeping configured connectors (listed at 0 docs, next sync = full pull),
  users, chat sessions/projects, settings, and the sync-event history. `POST /api/memory/reset`
  (auth-gated, 409 while any sync/cleanup runs) + a Settings **Danger zone** with a type-`RESET`
  confirm. Suite: **470 passed** (+4; catalog wipe/keep-gaps, full-stack API reset + refuse-while-
  syncing, pg parity), 12 skipped, pre-existing eval-yaml failure unchanged. Verified live in Chrome:
  57 docs → 0, graph 0 nodes/0 edges, connector kept, re-sync repopulated 71 docs as a full pull.
- Reset made observable — as a background job (2026-07-20, user report: after clicking reset the UX
  gave no log/notification/way to know it finished, and reopening the drawer re-showed the confirm
  prompt). Reset now runs through `SyncManager.start_reset`/`_run_reset` (`kind="reset"`, sentinel
  source `"all memory"`), so it streams a log ("removed N documents, M entities, K edges → cleared
  vectors + FTS → ✓ reset complete"), opens the log modal on confirm, and appears in the activity
  bell + sync history (kind-aware labels). The logs SSE endpoint now serves manager-only jobs. Fixed
  the confirm-persists bug (DangerZone resets its armed state when the drawer closes). Reset↔sync
  mutual-exclusion guard added both ways. Suite: **473 passed** (+3), verified live in Chrome (log
  modal + bell + no re-prompt on reopen). **New house rule added** (Conventions): any API change
  must keep Swagger tags/summaries, the Postman + Bruno runbooks, and README/CLAUDE in sync.

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
