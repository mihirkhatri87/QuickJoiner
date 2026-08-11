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
- **Node for the frontend**: nvm-windows has v22.22.3 but it's NOT on PATH (PATH resolves to
  an old v16.10.0). Prepend per command: `$env:Path = "$env:APPDATA\nvm\v22.22.3;$env:Path"`,
  then `npm run build` in `frontend/` (slow machine: build ≈ 2–5 min; don't kill it early).

## Commands

```
qj init <org> [--provider anthropic|ollama|litellm]  # create workspace (~/.quickjoiner/<org>)
qj learn <path|url|"free text fact">          # ad-hoc ingestion / taught notes
qj connect <type> --name N -o key=value ...   # register a source (types: files git github gitlab
    [--share | --private]                     #   jira confluence azure_devops octopus onedrive
                                              #   grafana datadog dynatrace elastic web_scrape).
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
qj extract <file> [--full] [--out F]          # show exactly what the text extractor reads out of
                                              #   a file (per-slide for a deck). No workspace, no
                                              #   ingestion — tells a parser gap from a genuinely
                                              #   picture-only document.
qj onedrive login|status|logout <name>        # Microsoft 365 device-code sign-in for a OneDrive
qj onedrive learn <name> <url-or-path>...     #   connector; `learn` ingests ONLY what you point
                                              #   at (nothing is crawled). Web UI: Sign in with
                                              #   Microsoft on the connector plate.
qj skills list|show <name>                    # packaged expertise (open Agent Skills format).
qj skills install <folder-or-zip>             #   `list` marks each one ready / needs-N FOR YOU;
qj skills disable|enable <name>               #   `disable` stops offering one WITHOUT deleting —
qj skills remove <name> [-y]                  #   the answer for a personal ~/.claude or ~/.copilot
                                              #   skill, which QJ refuses to delete (another tool's
                                              #   files). install/remove are admin acts (a skill may
qj promotions mine|offer <doc> [--note ..]    # personal -> org, BY REVIEW. `offer` queues one of
qj promotions queue                           #   YOUR private documents; a reviewer (editor+) sees
qj promotions decide <doc> --approve|--decline#   it in `queue`, can read it there, and decides.
    [--note ..]                               #   Approving is a metadata FLIP, not a copy: same
                                              #   doc_id, same citations, same graph edges, no
                                              #   re-embed. Offering publishes nothing on its own.
qj skills secrets                             #   carry scripts). `secrets` shows which values
qj skills set-secret KEY [--value V]          #   you've set and which are missing — NAMES only,
    [--shared]                                #   never values; --shared = workspace-wide default
qj skills unset-secret KEY [--shared]         #   that anyone's own value still overrides.
qj browser login <url> / qj browser status [url]  # Playwright profile for credential-gated
                                              #   sites; `login` captures session cookies and
                                              #   VERIFIES the sign-in took (exit 1 if not),
                                              #   `status [url]` re-checks it later
qj eval <set.yaml> [--init] [--agent]         # grounding evals: retrieval metrics always
                                              #   (recall@k, MRR, threshold, refusal accuracy);
                                              #   --agent adds end-to-end behavior checks (needs LLM)
qj bench <pack.yaml> [--init] [--agent]       # speed/cost: per-stage retrieval latency (p50/p95)
    [--compare old.json] [--repeats N]        #   + embedder chunks/sec always; --agent adds answer
                                              #   latency + tokens/answer (needs LLM). An eval set
                                              #   works as a pack. --compare exits 1 on a >20%
                                              #   regression — the speed twin of `qj eval --compare`
qj drain-graph [name]                         # mine relationships for already-ingested docs whose
                                              #   deferred extraction never resolved (a moving-window
                                              #   connector never re-yields them, so no sync retries it)
qj regraph [name] [--with-triples] [-y]       # rebuild the knowledge graph from documents ALREADY
                                              #   ingested — no re-fetch, no re-chunk, no re-embed.
                                              #   For when the graph extractors changed but the
                                              #   documents did not. Deterministic by default (no
                                              #   LLM); --with-triples also re-mines relationships
                                              #   (one model call per doc). Docs ingested before
                                              #   their connector's payload was persisted keep their
                                              #   existing edges instead of being rebuilt.
```

Workspace layout: `catalog.db` (SQLite — **config settings + connector sources now live here**,
not a YAML file), `lancedb/` (vectors), `repos/` (git clones). A legacy `config.yaml` is
auto-migrated into SQLite on first load and renamed `config.yaml.migrated`.
Override location with `--workspace` or `QJ_WORKSPACE`.

Docker: `docker compose up` (or `docker build` + `docker run -p 8787:8787 -v qj-data:/data`).
All state persists in the `/data` volume; first-boot provider defaults via `QJ_PROVIDER`, then
tune everything from the UI. Lean image by default; `--build-arg WITH_BROWSER=1` bundles Playwright
for the web_scrape browser fallback. Host Ollama reachable at `host.docker.internal:11434`.
**`.gitattributes` pins `*.sh` to LF** — a Windows checkout with `core.autocrlf=true` otherwise
rewrites `docker-entrypoint.sh`'s shebang to `#!/bin/sh\r`, and the container dies in a restart
loop on `exec … no such file or directory`, naming a file that plainly exists (hit live).
**Chromium needs memory + `/dev/shm` headroom (diagnosed live, 2026-07-30).** A browser crawl in
the container repeatedly killed the whole server mid-sync; every orphaned run then surfaced as
"interrupted — server restarted before it finished", and the UI's log stream died with it and
reported the browser's bare "network error" as though the *sync* had failed. Measured rather
than guessed: `docker inspect` showed `ExitCode=0`/`OOMKilled=false` (a clean SIGTERM, NOT a
cgroup OOM), while sampling `docker stats` through a crawl showed memory climbing to
**1.195 GiB of a 1.916 GiB VM** across 13 Chromium processes, then the container vanishing at
exactly that peak. Root cause is host-side: Docker's VM was running with only ~2 GB **on a 64 GB
machine** (no `.wslconfig`); the Docker *daemon itself* then died mid-build with an
`EOF`/500 — the same starvation one level up — and came back allocated 16.7 GB, after which the
kills stopped. So the operational rule is simply **give Docker real memory before crawling with
a browser** (4–8 GB+); a ~2 GB VM cannot host Chromium plus the embedder. Two code-side reductions ship regardless: `_CHROMIUM_ARGS = ["--disable-dev-shm-usage"]`
applied at the single `_context_opts` choke point (Docker gives a container **64 MB** of
`/dev/shm` and Chromium keeps renderer shared memory there — exhausting it crashes tabs with
nothing in the error naming shared memory; the flag backs it with /tmp instead), plus
`shm_size: "1gb"` in `docker-compose.yml` as belt-and-braces; and `_crawl_via_browser` now
**reuses ONE page** for the whole crawl instead of opening a tab per URL — each tab is its own
renderer process, so per-URL create/destroy kept a pool of them alive. Cookies live on the
context and a navigation resets page-level JS state, so reuse is equivalent here.
**Dockerfile layer order is dictated by rebuild cost** (restructured 2026-07-30, measured):
dependencies + the ~250MB Chromium install from `pyproject.toml` alone come FIRST, and
`COPY quickjoiner` / `COPY --from=ui` come after, so a source edit can't invalidate them. A
stub `quickjoiner/__init__.py` satisfies hatchling during the dependency install, then the real
package lands via `pip install --no-deps .` — so the dependency list is still declared exactly
once, in `pyproject.toml`, with nothing duplicated into the Dockerfile. BuildKit cache mounts
back pip and npm. Measured on this machine: one-line Python change **311s → 10.9s**, no-op
build 3.2s, frontend-only ~33s (Vite build dominates); image export alone fell 67s → 1s.
Previously BOTH source copies preceded the install layer, so every edit re-downloaded Chromium.

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
  adapter, `PostgresCatalog` the psycopg-pool adapter. Its `_pg` translation escapes **`%`→`%%`
  first**, then `?`→`%s` (that order, so the markers it writes are not re-escaped): psycopg reads
  a bare `%` anywhere in the statement — **including inside a `--` comment** — as the start of a
  placeholder and raises `incomplete placeholder`, which SQLite can never reproduce. Not
  hypothetical: a `-- 57% degree-1 nodes` comment in `graph_snapshot` broke the whole-graph view
  on Postgres only (2026-08-04), and that function's own comment had until then *asserted* no
  literal `%` appears in the catalog SQL. Escaping at this one choke point covers the inline
  query strings too, which live in method bodies and can't be enumerated — pinned by a pure
  `test_postgres_placeholder_translation_escapes_percent_signs` in `tests/test_catalog.py` so the
  next one is caught **without Docker**. Query *values* never pass through here (a LIKE pattern is
  a bound parameter), so escaping statement text cannot affect a wildcard.
  `base.py` has the `CatalogBackend`/
  `StoreBackend` Protocols. `PgVectorStore` mirrors `KnowledgeStore` on a pgvector `chunks` table
  (HNSW cosine). Cloud deps are the `cloud` extra; `docker-compose.cloud.yml` runs app+pgvector.
  Postgres path verified by `tests/test_pg_backend.py` (env-gated on `QJ_TEST_DATABASE_URL`,
  incl. a SQLite-parity retrieval test). ⚠ **It is env-gated, so it only runs when someone points
  it at a real Postgres — do that whenever Docker is available.** Two defects had accumulated
  behind that gate by 2026-08-04: the `%` bug above, and a stale test still asserting the
  pre-2026-07-21 prune semantics (it pruned an unfinished run, which the durable-pause work
  deliberately stopped). Static review had passed both. `store.py` (LanceDB, cosine; score = 1 − distance;
  **LanceDB disk reclamation, 2026-07-23**: LanceDB is copy-on-write — every `delete`/`add`/upsert
  writes a new table version and leaves the superseded data files + version manifests on disk, and
  **nothing pruned them**, so a workspace grew without bound across syncs / clean re-syncs / connector
  cleanups — observed live at ~55k dead versions / **54 GB** of manifests behind a ~250 MB live
  corpus (a `reset` already reclaims, since `drop_table` removes the table dir outright; the leak was
  ongoing sync churn). `KnowledgeStore.compact()` runs `table.optimize(cleanup_older_than=0,
  delete_unverified=False)` to prune every version but the latest + compact fragments; the safe
  `delete_unverified=False` default means it **never removes another source's in-flight files on the
  shared table**, so it's safe while other sources sync concurrently (`aggressive=True` is opt-in for
  a single-process maintenance pass only). `maybe_compact()` self-throttles via a
  `<lancedb>/.compacted_version` marker — it only optimizes once `_COMPACT_EVERY_VERSIONS` (500) table
  versions have accumulated since the last pass (matching LanceDB's "optimize every ~20 modification
  ops" guidance), so a stream of small incremental syncs doesn't pay for a full optimize each time.
  Wired in: `pipeline.ingest` calls `maybe_compact` (throttled) after each batch alongside
  `ensure_ann_index` (both via `getattr` ⇒ absent on `PgVectorStore`, where autovacuum handles it);
  `delete_source` (clean re-sync purge / connector cleanup — a big explicit delete with no following
  ingest) calls `compact()` immediately; `reset` clears the throttle marker. Best-effort throughout
  (any optimize failure leaves data correct, just larger — never breaks a sync). Tests:
  `tests/test_store_compaction.py`), `catalog.py`
  (SQLite: **workspace config in a `settings` table, connector sources in the `sources` table**
  (columns owner/shared/configured/sync_interval), document hashes, sync state, users/tokens,
  `sync_events` (the rolling sync/cleanup history behind the notification menu, incl. a `kind`
  column — see `sync_manager.py`; late-added columns live in the shared `_MIGRATION_STATEMENTS`
  applied best-effort by **both** adapters on open). **`reset_knowledge(include_gaps=True)`** is the
  global memory reset: DELETEs all documents/edges/entities/aliases/graph_pending/sync_state + the
  ingestion-bucket source rows (`configured=0`) + gaps, keeping configured connectors, users,
  sessions and settings (neutral `?`-SQL ⇒ both backends; caller wipes vectors via `store.reset()`);
  `load_config`/`save_config`/`list_source_configs`/`write_source` are the config API, with
  one-time YAML migration.
  **The settings blob is SPARSE — this is what lets a retuned default reach a workspace that
  already exists (2026-07-31, user-reported):** `save_config` dumps with
  `exclude_defaults=True`, so only fields that genuinely differ from the shipped default are
  stored and an untouched field stays governed by `config.py`. It used to dump every field,
  which materialized whatever value was in force the first time a workspace saved and **pinned
  it forever** — so plan 05's `min_score` 0.55→0.64 retune, and every default change before it,
  reached new workspaces only. Measured on the live corpus two days after that retune shipped:
  still running 0.55, so its whole measured benefit (3 of 12 refusal near-misses gated at zero
  recall cost) was unrealised. The trade-off is deliberate and one-directional: a value
  explicitly set to *today's* default is indistinguishable from an untouched one and will follow
  a future retune — which is exactly what the Settings drawer already tells the user ("default"),
  and strictly better than a shipped retune that silently applies nowhere.
  A blob written by the old code has everything materialized, so "never set" and "deliberately
  set" cannot be told apart in it — **except** for a value that equals a default this repo has
  since superseded. `config.SUPERSEDED_DEFAULTS` (pure `reconcile_superseded_defaults`) lists
  those four (`retrieval.min_score` 0.55, `retrieval.reranker` "none", `graph.triple_workers` 4,
  `retrieval.rerank_candidates` 24 — the only defaults ever changed, confirmed against git
  history, not memory) and
  `_adopt_shipped_defaults` prunes them ONCE per workspace, guarded by a `config_defaults_epoch`
  settings row against `config.DEFAULTS_EPOCH`. It **logs every field it moves** at INFO —
  retrieval behaviour must never change silently — and keeps anything else, so the live
  workspace's deliberate `graph.triple_workers: 8` survives untouched. Verified against a copy
  of the real 11-connector workspace: min_score 0.55→0.64 announced, triple_workers 8 and the
  already-current reranker untouched, all connectors intact, idempotent on the second load.
  ⚠ **When you change a default in `config.py`, add the old value to `SUPERSEDED_DEFAULTS` and
  bump `DEFAULTS_EPOCH`** — that is the step that carries the change to anyone who has not
  re-saved since (after any settings write the blob is sparse and needs no entry).
  `GET /api/settings` is unaffected: it dumps the in-memory `Config`, which is always
  complete), `embedder.py` (fastembed `BAAI/bge-small-en-v1.5` default;
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
  lazy — the ONNX model loads on first `rank()`, not at construction, so startup/build_context
  is free; failures degrade to RRF order; `QJ_DISABLE_RERANKER=1` env kill-switch, set by the test
  suite to stay offline). `factory.create_store` wires `retrieval` + reranker into both stores.
  **The default cross-encoder is the INT8 build (S4a, 2026-08-11, closing the last of S4's
  levers):** fastembed's catalogue carries only the fp32 `onnx/model.onnx`, so
  `_register_quantized` uses `add_custom_model` to register the SAME HF repo pointed at its
  `onnx/model_quantized.onnx` (23MB vs 91MB) — it still downloads and caches through the ordinary
  fastembed path, honouring `FASTEMBED_CACHE_PATH`. Measured over the live corpus's fused pools on
  the 32-case eval set: **−28.6% on the rerank stage, faster on 32/32 queries** (paired ratio
  0.63–0.80), with recall@k, grounded recall, MRR, hop coverage and refusal accuracy **all
  byte-identical** — `qj eval --compare` reports delta 0.000 on every watched metric. Three
  variants of the same repo were measured, not assumed: `model_quantized` 918ms, `model_int8`
  1012ms, `model_uint8` 1517ms against fp32 1474ms in that run — identical 23MB file sizes, very
  different speeds, so the file is named explicitly.
  ⚠ **How that −28.6% was arrived at is the transferable part.** Two earlier framings of the same
  experiment (in-process, then a same-session A/B through `apply_config`) both ran fp32 first and
  both reported **−38%/−41%** — and the *untouched embedder*, carried as a control, came out **25%
  faster** in the second arm of the second one. This machine drifts under sustained ONNX load, so
  an arm-at-a-time comparison quietly hands ~10 points of drift to whichever arm ran later, and
  nothing in the numbers says so. The shipped figure is **paired and order-alternated** (both
  encoders resident, arms swapped per repeat, per-query ratios) which cancels monotonic drift to
  first order. Same reason `qj bench --compare` against the 01:44 baseline is NOT the evidence
  here: it flagged +12.5% p50 while reporting the embedder **64.5% slower** in the same run —
  a machine difference, not a code one. **Carry a control variable through any latency A/B on this
  box, and distrust any cross-run bench comparison whose untouched stages moved.**
  Whole-query effect follows the stage's share (65–76% depending on the run), i.e. roughly −19–22%.
  **The fp32 fallback is load-bearing, not belt-and-braces:** an existing install already has the
  91MB fp32 model cached, so an upgrade able to reach only the new file would turn an offline
  machine's *working* reranker into a silent degrade-to-RRF — a quality regression caused purely by
  upgrading. `_candidates()` therefore returns `[int8, fp32]` for the default and `[pinned]` for an
  explicit `retrieval.reranker_model`, which is never second-guessed. Both behaviours are pinned by
  tests **verified to fail with the fallback removed**. Honest limit: quantization perturbs scores,
  so the returned top-8 **order differs on 26 of 32 queries** — MRR being unchanged means the
  expected document holds its rank in every answerable case and the reshuffling is among candidates
  the eval set has no opinion about, which is a bound on what 20 answerable cases can see rather
  than a clean bill of health.
  **`retrieval.rerank_candidates` is a RECALL knob before it is a cost dial (24 → 16, S4,
  2026-08-10):** the reranker only ever sees `ordered[:rerank_candidates]`, so a candidate the
  fused order buried below that depth is invisible to it and no amount of relevance brings it
  back. Measured on the live 57,659-chunk corpus over the 32-case eval set, simulating every
  depth from ONE cross-encoder pass per candidate (scores are independent per candidate, so the
  whole curve comes out of a single scoring run) and **verified to reproduce `store.search`
  exactly on 32/32 queries** before any of it was believed: reranking off → recall 0.900 /
  MRR 0.775; depth 4–10 → 0.900 / 0.900; **depth 12 → 0.950 / 0.950**; depth 16, 20, 24 and 32
  → 0.950 / 0.925. Quality is **flat from 12 to 32**, so the shipped 24 was doing twice the
  necessary work. The one structural feature is the cliff below 12, and it has a name —
  `connector-nautical-pubsub`'s expected source sits at fused rank 12, the deepest in the set,
  so shallower depths never show it to the model at all. 16 rather than the measured-best 12 is
  deliberate margin: too shallow costs *recall*, too deep costs only latency, so the default
  does not park on a measured cliff (same reasoning as the `min_score` retune above).
  Live before/after on the real workspace, both gates run: `qj eval --compare` gives recall
  0.950, grounded recall 0.950, MRR 0.925 and hop coverage 0.750 **unchanged to three
  decimals**, and `qj bench --compare` gives rerank p50 **1628 → 1066 ms (−35%)**, whole query
  p50 **1977 → 1401 ms (−29%)** and p95 **2564 → 2029 ms (−21%)**, with `reranked: 16.0`
  confirming the depth actually moved. (The same bench run shows the embedder 11% faster; that
  is machine variance, not this change — nothing here touches ingest.)
  ⚠ **The one watched metric that moves is `refusal_accuracy` (0.25 → 0.167), and it is
  noise, recorded rather than tuned to**: it is a single case (`unlearned-competitor-pricing`)
  which leaks at depths 8, 10, 12, 16, 20 **and 32** and is correct at exactly 24 — non-monotone
  in depth, so it is not a property of depth 24. The mechanism is that the gate reads the dense
  cosine while reranking decides which chunks occupy the returned top-k, so a high-cosine chunk
  can be pushed out of the window by luck; "improving" refusal that way is an artifact, not
  judgment, which is why the deeper default was not kept to preserve it.
  Per-candidate cost is linear in candidate **length** as well as depth (measured 4.3 ms at
  185 chars, 9.2 at 464, 19.1 at 929, 30.3 at 1394, 69.3 at 2789; live corpus median chunk 1013
  chars, and **18% of chunks already exceed the model's 512-token cap** and are silently
  truncated by the tokenizer). A length cap is therefore a real second lever, deliberately NOT
  shipped, and **re-measured in S4a (2026-08-11) with a sharper test that settles it**: judged by
  ORDER IDENTITY against the untruncated ranking rather than by whether metrics happen to hold,
  because 20 answerable cases cannot distinguish a safe cap from a lucky one. The tokenizer cuts
  at **512 tokens** (`direction=Right`), so a cap above that boundary is *provably* lossless and
  one below it is a genuine trade — and the boundary is empirically **2000 chars: identical top-8
  on 32/32 queries**, versus 8/32 at 1400 and 1/32 at 1000. But it buys nothing measurable, because
  only **4%** of live candidates exceed 2048 chars: cap 2000 came out 1380ms against 1535 uncapped
  while cap **1400 came out SLOWER than uncapped** (1636ms), which puts run-to-run noise at ~±10%
  and makes the apparent win unquotable. The large savings are all below the token cap and all
  lossy — cap 600 is −55% for MRR 0.925 → 0.892, past the 0.02 `--compare` tolerance — and they
  wobble rather than degrade (hop coverage *improves* 0.750 → 0.875 at every cap ≤1400 while MRR
  dips; with 20 answerable and 8 multi-hop cases every one of those is a single case moving rank).
  So it stays parked behind a bigger eval set (#19), now with numbers rather than a hunch.
  **The more useful finding is why the stage costs what it does:** the median candidate in the live
  fused pools is **1658 chars ≈ 415 of the model's 512 tokens**, and cost is quadratic in sequence
  length (19 ms/candidate at 300 chars vs 86 at 2000) — so per-candidate cost here is set by *chunk
  length*, not by model choice, which makes the remaining headroom a chunking question (AI #2,
  AST-aware chunking) rather than a reranker one. Also measured and rejected as levers: ONNX thread
  count (the default beats every explicit setting — 1684 ms vs 2100 at 4 threads, 5350 at 1),
  batching (fastembed already puts the whole depth in one forward pass at `batch_size=64`), the
  three *other* fastembed cross-encoders (`jina-reranker-v1-tiny-en` is 21% faster but costs MRR
  0.925 → 0.863 — 3× the tolerance — and is *larger* on disk at 0.13GB than the incumbent's 0.08,
  so "smaller CE" was wrong twice; `jina-turbo` and `MiniLM-L-12` are worse on quality AND slower),
  and **early exit on an already-stable fused head, which is empirically dead**: reranking changed
  the returned top-8 on **32/32** queries, so there is no stable-head population to exit on — and
  the same run shows why the stage earns its keep at all (MRR 0.775 without it vs 0.925 with).
  `KnowledgeStore.ensure_ann_index()` builds a LanceDB IVF index past `retrieval.ann_min_rows`
  (pipeline calls it after each ingest batch). **Graph-expansion retrieval** (`retrieval.graph_expansion`,
  on): `catalog.graph_expand(seed_doc_ids)` finds documents one knowledge-graph hop from the grounded
  hits (via shared entities) and `search_memory` appends them as a "RELATED via knowledge graph"
  section — the multi-hop / cross-source channel. It runs ONLY when there are already grounded hits,
  so it never turns a refusal into an answer (grounding gate untouched).
  **Cross-source identity bridges (`same_as`, 2026-07-21, roadmap #24):** entities are keyed
  `type:name` and resolution merges same-type only, so `service:connector` / `repo:connector` / the
  pipeline that builds it were disjoint islands (measured pre-reset: 158 exact service↔repo name
  matches, 18 multi-source entities in 3,580). `ingest/bridges.py::compute_same_as_bridges` (pure)
  emits deterministic cross-type `same_as` edges between `service`/`repo`/`project`/`pipeline`
  entities whose names — or aliases, incl. **every connector's `aka` option** ("also known as",
  on all 13 types via a guarded append after `FORM_SPECS`; persisted by `upsert_source` →
  `_declare_aka_aliases` as entity aliases so they also feed `resolve_entity`/expansion/suggest for
  any source, and drive bridges wherever the source maps to a bridgeable entity) — match after
  normalization. `aka` follows the
  **connector-name editability rule**: settable at creation, locked once the connector has learned
  documents (a later alias *removal* could not be un-declared consistently) — generic
  `lock_after_sync` flag in `FORM_SPECS`, enforced in the PATCH endpoint (409 on a changed value,
  unchanged round-trips fine via `_norm_opt` list/string normalization) and rendered
  disabled-with-reason in `EditConnectorModal`. Guards: ≥5 alnum chars,
  all-generic names skipped (`_GENERIC_TOKENS`), groups >6 members skipped, cross-type only.
  **Honesty:** a bridge is an inference — empty `evidence_doc_id` (never citable, corroboration 0),
  self-describing `detail`, its own lowest-tier `name-bridge` confidence class (0.30 in
  `confidence._BASE`; min-rule caps any chain crossing one), excluded from `gc_orphan_entities`'
  keeps-alive rule (dangling bridges swept). `catalog.refresh_same_as_bridges()` is a full derived-
  layer recompute, called best-effort by `pipeline.ingest` after any batch with adds/updates;
  `graph_path` traverses bridges natively, `graph_expand` extends its seed entities across them but
  still returns only real cited documents. A bridge, deliberately NOT a merge: one row to delete if
  wrong. **Branch bridge (same-type, 2026-07-23):** a second pass bridges `branch:<repoA>/<x>` ⇔
  `branch:<repoB>/<x>` when the repo parts belong to the same **repo family** (`_repo_family_finder`
  union-find over repo names + aliases) — so a GitLab MR's source branch and a TFS work-item dev-link
  branch join even when the repo is spelled differently on each side. Guard `MAX_BRANCH_GROUP`=8; the
  repo-name-parity case needs no bridge (ids already equal). Tests: `tests/test_bridges.py`.
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
  **`graph_relations(rel, src_type, dst_type, limit)` (2026-07-31, same neutral `?`-SQL, no
  schema change)** — every edge of ONE relation shape, optionally constrained by the entity
  type on each end, ordered by destination then source. The enumeration read the graph could
  not previously serve: `graph_neighbors` answers "what is attached to this one thing" and
  `graph_path` "how do these two connect", but "every team with its members" is neither —
  it is one relation across the whole graph. Backs the `graph_relations` agent tool.
- `quickjoiner/ingest/` — `extract.py` (**the single text-extraction choke point**, 2026-07-22):
  `extract_text(bytes, filename) -> str` turns any supported file into plain text — Word (`.docx`),
  PowerPoint (`.pptx`), Excel (`.xlsx`) and PDF (`.pdf`) via lazy office/PDF parsers
  (python-docx/python-pptx/openpyxl/pypdf), plus Markdown/text/JSON/CSV/code decoded directly and
  HTML stripped to text (keeping `<img alt>` captions).
  **HTML tables are rendered as markdown pipe rows, not flattened (2026-07-31,
  user-reported via "list all teams with their members").** `soup.get_text()` puts every
  cell on its own line, which destroys the table: the column each value belonged to is
  gone and — worse — a **blank cell simply vanishes**, so every later value in that row
  shifts left into the wrong column. Measured on the live corpus: a member row missing
  only its email rendered its Location where Phone belonged, and nothing in the stored
  text revealed it (you could only recover the truth by diffing against a row that
  happened to be complete). `render_html_table` keeps the header, the alignment and empty
  cells, escapes `|`, expands `colspan` to padding, and caps rows/cell length
  (`_MAX_TABLE_ROWS` 500 / `_MAX_CELL_CHARS` 300, truncation stated). Two deliberate
  shape rules: a **single-column** table is page layout, not data, so it degrades to its
  text rather than wrapping prose in table syntax; and only **leaf** tables render (one
  containing another is skipped) because old intranet apps wrap real data tables in
  layout tables, and rendering the outer one would bury the data in a single cell. Runs
  AFTER the `<img>` pass so an image inside a cell still contributes its caption and
  still reaches the vision seam. Each row is also a self-contained line, which chunks and
  embeds far better than a vertical stream of orphaned cells. Consumed by
  `ingest/tables.py`.
  **A header row may trail off into spacer columns (`_HEADER_BLANK_TOLERANCE`, 2026-08-05,
  user-reported).** Header detection accepted `<th>` or a first row where EVERY cell was
  populated — and a real catalogue roster's header ends in an empty actions/spacer column
  (`Name | Role | … | Location | `), so `all(rows[0])` was false and the header was demoted
  to a data row. The table then rendered **headerless**, which is silent and total: with no
  header there are no column names, so `_column_types` types nothing and `ingest/tables.py`
  emits zero edges from a complete member table. Measured on the live Plumber corpus: the
  Caffeine page's 7 members produced **0** person→team edges and 0 email aliases; with the
  fix, 4+ `works_on` edges and the emails as person aliases. The rule is now "mostly
  populated" — at least 2 filled cells AND no more than `_HEADER_BLANK_TOLERANCE` (2) blanks
  — which is deliberately a *tolerance*, not a licence: a wide data row carrying two values
  still fails it and leaves the table headerless, because promoting a data row would delete
  it from the body entirely. Pinned both ways in `tests/test_tables.py`
  (`…_spacer_column_is_still_a_header`, `…_mostly_empty_first_row_is_data_not_a_header`).
  **Office shape-tree recursion
  (2026-07-24, user-reported):** `slide.shapes` yields only TOP-LEVEL shapes, so every label
  inside a **grouped** diagram was dropped — silently, with the extraction still reporting
  success. A PowerPoint architecture diagram is precisely a group of labelled boxes, so an
  attached architecture deck extracted to ~nothing and the agent (correctly) said it had no
  content; that is what the user hit. `_pptx_walk` now recurses into groups (depth-capped,
  order-preserving, deduped) and `_pptx_chart_lines` reads embedded chart titles/series/categories
  (chart text lives in a chart part, not the shape).
  **The object model alone is not enough (2026-07-24, second report — "there's lots of text in
  this file but the parser is missing everything", and they were right).** Read from the source:
  `pptx/oxml/shapes/groupshape.py::iter_shape_elms` yields only children whose tag is in a
  hard-coded whitelist — `p:sp`, `p:grpSp`, `p:graphicFrame`, `p:cxnSp`, `p:pic`, `p:contentPart`.
  **`mc:AlternateContent` is not in it**, and PowerPoint wraps a shape in that whenever it uses a
  feature needing a legacy fallback (icons, 3D, ink, newer effects, much SmartArt) — so those
  shapes, and all their text, are **invisible to python-pptx** and were lost silently. Reproduced:
  a 2-shape slide where `len(slide.shapes) == 1`. Extraction is therefore now **three passes,
  merged and deduped**: (1) the object model, where it is richest (knows a table from a text box,
  reads charts, visual order); (2) `_drawingml_lines` — a raw sweep of the slide XML for every
  `a:p`, which cannot miss a shape type because it does not know about shape types. It descends
  into exactly ONE branch of an `mc:AlternateContent` (Choice, else Fallback) since both carry the
  SAME content and taking both duplicates every line, and it emits `a:tbl` rows joined `a | b | c`
  to match the structured pass so the two dedupe instead of yielding a joined row *and* its loose
  cells; (3) `_pptx_related_texts` — parts the slide only *references*: **SmartArt**
  (`diagramData`), **charts** (DrawingML runs + non-numeric cached `c:v` series/category names),
  and **embedded workbooks/documents** (a pasted Excel table is a whole xlsx package, parsed by
  recursing into the same extractor). Pass 2 is appended rather than interleaved — exact visual
  position is unrecoverable there, and completeness beats ordering for retrieval.
  **Damaged packages (2026-07-24, live failure on the user's real deck):** `qj extract` on it
  died with `Bad CRC-32 for file 'ppt/media/image7.png'` — Office files are zips, and
  python-pptx/python-docx refuse to open the WHOLE package when one member is corrupt, so a
  single damaged image cost every slide's text although text and images share nothing but the
  container (PowerPoint itself opens such files fine, so the deck looks perfect to whoever sent
  it). `_zip_text_members` reads members individually, skips unrecoverable ones, and retries a
  needed member with CRC verification disabled (a checksum mismatch does not mean the bytes are
  useless); `_salvage_pptx`/`_salvage_docx` then run the raw DrawingML/WordML sweep over
  `ppt/slides/*`, notes, diagrams and charts — never touching `ppt/media/*`. Wired as a fallback
  when the library raises: salvaged text wins, and a genuinely unreadable file still raises
  `ExtractionError` as before. Reproduced end-to-end in tests (a deck whose `Presentation()` load
  raises still yields both slides' text, with slide structure preserved).
  Same class of loss fixed in Word: `_docx_textbox_texts` harvests
  `w:txbxContent` (text boxes/shapes are invisible to `document.paragraphs`) and headers/footers
  are read per section (document titles, classification markings, version stamps live there).
  Verified end-to-end, not just at the extractor: a grouped-diagram deck ingested via
  `POST /api/uploads/local` and its labels retrieved at 0.63–0.77, well clear of the 0.55 gate.
  Also **zip archives expanded in place**
  (2026-07-24: `ARCHIVE_EXTENSIONS`/`_extract_zip` — members route back through `extract_text`,
  so a .docx inside a .zip is parsed as a .docx, each under a `--- path ---` header that keeps
  its provenance; hard-bounded by member count / total uncompressed bytes / per-member size as
  the decompression-bomb guard, **nested archives listed but never opened** since depth is where
  bombs live, and every skip appended as an "archive notes" block rather than silently dropped),
  and a much wider text/code set (transcripts `.vtt`/`.srt`, `.adoc`/`.org`/`.tex`, `.ipynb`,
  `.proto`/`.graphql`, `.kt`/`.swift`/`.scala`/`.vue`/`.dart`/…). **Images are a named
  *not-yet*, not an unknown**: `IMAGE_EXTENSIONS` + `is_image()` + `_extract_image` route whole
  images to the same `ImageHandler` seam, raising a specific "image files can't be read yet —
  vision support is on the roadmap" when none is wired in, so an ingested image is reported
  honestly instead of landing as an empty document (AI_ROADMAP **#23.a** covers OneDrive images
  when vision ships). `supported_extension` excludes images deliberately — callers use it to
  decide whether to spend a read at all. Every ingestion surface funnels through it:
  the `files`/`git` connectors, the rolling `uploads` connector, and the upload endpoints. Defensive
  — a missing parser or corrupt/encrypted/image-only file raises `ExtractionError` (caller skips that
  one file, never fails a sync); size caps bound work. **Vision seam (text-first today, multimodal
  tomorrow):** an optional `ImageHandler = Callable[[bytes,str],str]` threads through `extract_text`
  and the office/PDF/HTML extractors, which already enumerate embedded images and call it — None
  today ⇒ pure text, images skipped honestly; the planned vision layer (`docs/plans/07-…`,
  AI_ROADMAP #23) supplies a handler built from a vision provider to describe diagrams/scanned pages
  into text with **no extractor changes**. `TEXT_EXTENSIONS`/`DOC_EXTENSIONS`/`CODE_EXTENSIONS`/
  `NAMED_TEXT_FILES` live here now (`files.py` re-exports them). Tests: `tests/test_extract.py`
  (per-format, HTML alt-text, corrupt→error, inert-vs-invoked vision seam).
  **`tables.py` — deterministic graph extraction from tabular content (2026-07-31,
  AI_ROADMAP-adjacent, user-reported).** A table is the most structured thing on a page and
  was the least mined: measured on the live 728-page internal service catalogue, **0 of 17**
  team pages produced a single edge, while the SAME pages' key-value blocks
  ("Teams: Caffeine") extracted fine. The LLM wasn't at fault — a flattened table is a wall
  of unlabelled values with no sentence linking a person to a team, so emitting nothing was
  the honest outcome. Structure was the whole difference, so this reads it directly, the way
  `code_graph.py`/`pubsub.py` read code and config. It runs on the **markdown pipe rows**
  `extract.py` now preserves, NOT on HTML, so it serves every source whose text carries a
  table (scraped pages, Confluence `body.view`, markdown, Office). Two things are read, and
  only two, because both are *stated* rather than inferred: **(1) typed columns** — a header
  names its column's type via `_HEADER_TYPES` (`Team`, `Repository`, `Environment`, … incl.
  a multi-word tail match so "Octopus Project" types but "Projected Cost" doesn't); a
  generic `Name` column is typed ONLY when an email column proves the rows are people, or
  when the table's own caption announces what it lists (`_CAPTION_TYPES`: "Teams" → team) —
  "Name" heads lists of services as often as lists of people, so anything less is a guess.
  **(2) The subject in the URL** — catalogue web apps carry the entity in the query string
  (`/TeamDetails?id=45&team=Autobots`), which is the page stating its own subject in
  machine-readable form; `subject_entities` reads only `_URL_SUBJECT_PARAMS` and only values
  that look like names (an opaque `?id=45` names nothing). That subject pairs with each row,
  which is what turns a members table with **no team column** into `person --works_on-->
  team`. `_PAIR_RELATIONS` maps an ordered pair of typed cells to its relation
  (person+team → works_on, team+repo → owns, service+environment → deploys, …) — a
  deliberate subset of `triples.RELATION_SIGNATURES`, **lockstep-tested** against it so this
  extractor can never emit a shape the vocabulary's own domain/range validation would reject
  (`builds` is exempt via `_DETERMINISTIC_ONLY_RELS`: connector-vocabulary, same footing as
  ADO's, no signature to check). **A person row's email becomes an ALIAS of that person** —
  the fix for a measured identity split on the live graph: only **1 of 875** person entities
  was shared across sources, because Plumber names people by email (`lthillet@opentext.com`),
  Confluence by display name (`Chris S`), TFS by full name (`Greyden Hochstetler`) — three
  disjoint node sets for the same humans. The table carries name AND email in one row, so
  aliasing them is definitional, not inferred. Honesty guards: booleans/numbers/`N/A`
  placeholders are never entities (`_NON_ENTITY_VALUES`), names are length-bounded, an
  untyped column contributes nothing rather than a guess, and an entity that took part in no
  edge is dropped (it is exactly what `gc_orphan_entities` sweeps). Bounded per doc
  (20 tables / 400 rows / 600 edges). Pure, same contract as its siblings:
  `(text, uri, src_id, title) -> (entities, aliases, edges)`; wired into `_sync_graph`
  beside `extract_pubsub_graph`, so it lands whether or not LLM extraction is enabled.
  Tests: `tests/test_tables.py` (19, incl. the blank-cell regression, layout-vs-data table
  shapes, caption typing vs guessing, the signature lockstep, and an end-to-end
  HTML → rows → edges → `graph_relations` pass).
  `pipeline.py` (**normalize → sha256 dedupe → chunk → embed → upsert;
  idempotent**; optionally injected a `triple_extractor`).
  **Graph-extractor version — refresh the graph on a plain sync, no re-embed (2026-07-23):**
  `GRAPH_EXTRACTOR_VERSION` (module constant, bump when the DETERMINISTIC extractors change —
  code_graph/pubsub/deps/ticket-keys or a connector's `metadata["graph"]` like ADO dev-links / GitLab
  MRs) + a per-doc `graph_version` column. In `_ingest_one`, an UNCHANGED doc whose stored
  `graph_version < GRAPH_EXTRACTOR_VERSION` re-runs `_sync_graph` (rebuilding edges from the
  freshly-fetched doc's text+metadata) and stamps the version, **skipping chunk/embed** — so a
  graph-only feature rolls out on the next ORDINARY sync (that re-provides the doc) instead of a clean
  re-sync (`stats.graph_refreshed` counts it). Existing docs default to `graph_version=0` (< current) so
  they refresh once, then settle. Cheap when LLM triples are off (deterministic only); with
  `graph.extract_triples` on, a version bump re-queues triples for re-fetched unchanged docs (one-time).
  Only reaches docs the connector actually **re-provides** that sync (full-refresh connectors
  files/git/confluence/ADO-recent-sprints do; watermark-incremental github/gitlab/jira only re-fetch
  changed docs, so their unchanged docs refresh on a clean re-sync). Version stamped on full ingest
  (`upsert_document(graph_version=…)`) and after the triple drain (`_apply_triples`). `chunkers.py` (markdown/code/prose aware;
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
  need tree-sitter);
  **`layers.py` — the deterministic architectural layer of a code entity (2026-08-10,
  AI_ROADMAP #29).** `classify_layer(path)` reads the role a file's own path states —
  `api | service | data | ui | utility | infra | test | vendor` — and returns **None** when
  it states nothing, which is the honest majority case. Never LLM-derived (I3), so it works
  keyless and offline and is identical on every machine. Rides the entity tuple as an
  optional **4th element** from `code_graph` (other extractors' 3-tuples are untouched;
  `_persist_graph` unpacks by slice), stored on `entities.layer` and resolved by
  **precedence, not last-writer-wins** — a production layer beats `test` beats `vendor`, so
  a symbol defined in both `CustomerService.cs` and `CustomerServiceTests.cs` stays a
  service symbol regardless of which document a sync reaches first. Classified from the
  FULL uri, not the `defines` edge's `detail`, which `_short_path` truncates to 80 chars.
  **The taxonomy is a superset of the roadmap's because the corpus demanded it, and this is
  the part worth remembering.** Measured on the live 15,469 `defines` paths *before* writing
  any rules: the roadmap's first-named signal (directories like `/controllers`,
  `/repositories`) tags **3.3%** — this codebase is organised by DOMAIN (customeraccounts,
  sales, pricing, quotes), which is normal for enterprise .NET and fatal to a directory-only
  rule. Filenames are 4× better (13.1%), and even combined the six layers the roadmap names
  reach only **10.4%** of production files — an attribute that sparse cannot make a graph
  "read as an architecture". The two dominant categories are ones it never mentions: **test
  scaffolding is 33.7%** of defining files and **vendored code 16.0%** (one
  `jquery-1.4.4.js` contributing symbols like `doscrollcheck` as first-class org entities).
  With `test` and `vendor` added, coverage is **59.9% of defining paths / 54.8% of symbol
  entities** — a different feature from the one specified, serving the same three payoffs.
  **Ambiguous words are left untagged on purpose**: `handler` is an HTTP handler in one
  codebase and a CQRS command handler in the next, `model` is domain/persistence/view
  depending on the shop, and `event`/`command`/`request`/`response`/`dto` are message
  shapes rather than layers. A wrong layer is invisible once stored; untagged is honest.
  `view`/`page`/`screen` survive only as DIRECTORY signals after `Model/Page.cs` — an
  OpenAPI pagination model — was mislabelled `ui` in validation. `is_vendored` matches whole
  path SEGMENTS and library filenames, never a substring, because the corpus contains
  `GetOrderByVendorCodeResponseServiceModel.cs`, an org file about a *vendor code* business
  concept. ⚠ **Two defects were caught only by scoring against the real corpus, not by unit
  tests**: `_segments` lowercased every segment, so the CamelCase regex that reads the
  trailing role word matched nothing on multi-word names and collapsed test detection from
  31.9% to **0.5%** (short fixture names survived it); and .NET names test PROJECTS rather
  than folders (`AppRiver.Nautical.Domain.Tests`), which exact segment matching missed
  entirely — `_is_test_dir` now checks dot-separated parts. Surfaces: `entities.layer`
  (migration, both backends), `src_layer`/`dst_layer` on `_EDGE_SELECT` and the graph
  snapshot's nodes, and a **Tests & vendor** toggle in GraphView that states how many nodes
  it hides rather than quietly shrinking the graph (opt-in; nothing hidden by default).
  `GRAPH_EXTRACTOR_VERSION` 5→**6** carries it onto already-ingested code with no re-embed.
  Tests: `tests/test_layers.py` (13). NB an imported `module` entity gets NO layer — that
  would be a property of the module's own source, and taking the importer's path would tag
  every library with its consumer's layer;
  **`pubsub.py` (2026-07-17) emits runtime-coupling edges deterministically** —
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
  **Draining deferred graph work a connector will never re-provide (`drain_pending_graph`,
  2026-07-30, AI_ROADMAP #25):** a `graph_pending` row is normally retried by the next sync that
  re-yields the document — which never comes for a connector ingesting a moving window (a TFS
  work item aged out of every team's recent-sprint slice; the live workspace had **1,331** such
  documents). Those documents keep their chunks, vectors and citations and have **no edges at
  all**, because `_sync_graph` defers a qualifying document's ENTIRE graph — connector-supplied
  `metadata["graph"]` included — until its LLM triples resolve. `drain_pending_graph(source_id=,
  control=, log=)` re-reads the text that was actually indexed (`store.get_documents_chunks`,
  one filtered scan for the whole work-list; the contextual-chunking breadcrumb is stripped back
  off so the rebuilt text is the document, not its provenance line repeated per chunk), rebuilds
  a synthetic `Document`, and runs it through the SAME `_sync_graph` → `_resolve_pending_triples`
  path — no connector round-trip, and pause/stop/staging work exactly as in a sync. **Faithfulness
  is explicit, not assumed:** `mark_graph_pending` now stores the deterministic payload
  (`graph_pending.graph_json`) so a drained document is rebuilt *completely* (dev-links,
  hierarchy, deps edges), while rows predating that column can only be re-mined from stored text
  and are counted+logged separately (`DrainStats.faithful` vs `text_only`) rather than folded into
  a total that would read as full recovery. A document whose chunks are gone stays queued
  (`missing_text`), never resolved with an invented empty graph; `sweep_orphan_graph_pending`
  drops rows whose document was deleted. Surfaces: `SyncManager.start_drain` (`kind="drain"`,
  sentinel source `"graph relationships"` — refuses while any other job runs, and syncs refuse
  while it runs, since it writes edges across sources; refuses outright when
  `graph.extract_triples` is off rather than silently clearing the queue),
  `GET /api/graph/pending` + `POST /api/graph/drain[?source_id=]`, `qj drain-graph [name]`, and a
  Settings → Knowledge graph panel that appears only when the queue is non-empty. Tests:
  `tests/test_triples.py` (faithful vs text-only rebuild, scoping, orphan sweep, missing-text
  left queued, breadcrumb strip), `tests/test_sync_manager.py`, `tests/test_api.py`.
  **Rebuilding the graph without re-fetching or re-embedding (`rebuild_graph`, 2026-08-06,
  user request — CLI `qj regraph`, `POST /api/graph/rebuild`, a per-connector action + a
  Settings panel in the UI):** the case is "the graph extractors changed, the documents did
  not". A re-sync re-downloads everything and re-embeds whatever text shifted to reach the
  same edges; this walks the documents already in the catalog, re-reads the text that was
  actually indexed (the same `_stored_texts` path the drain uses, breadcrumb stripped) and
  re-runs the very same `_sync_graph`, so a rebuilt graph is built by exactly the code an
  ingest would have used. `replace_doc_edges` is a delete-then-insert keyed on
  `evidence_doc_id`, so iterating every document is **self-purging per document** — no
  source-scoped edge delete is needed (and none exists: `edges` has no `source_id`).
  ⚠ **Why this is not simply "delete the edges and redo them", measured before building it:**
  a connector's own structural claims — ADO dev-links and work-item hierarchy, GitLab
  MR/branch joins, Jira issue links, Octopus deployments — are computed while **FETCHING**,
  so skipping the fetch is precisely what loses them. On the live 100,341-edge graph,
  **27% of all edges** (5,432 in connector-only relations plus 21,803 tracker-sourced
  `part_of`/`related_to`/`deploys`) could not be re-derived from stored text, and
  `documents.metadata_json` held only the `display` block — **zero** documents persisted a
  graph payload. So two things ship together: `documents.graph_json` captures
  `Document.metadata["graph"]` at ingest (and backfills onto UNCHANGED documents via
  `_capture_graph_payload`, which is why `GRAPH_EXTRACTOR_VERSION` went 4→**5** — the edges
  did not change, the bump exists purely to carry the payload onto an existing corpus with
  no re-embed); and each document then takes one of two reported paths —
  **faithful** (payload present ⇒ authoritative replace) or **preserved** (no payload ⇒ its
  existing edges are read back via `catalog.edges_for_document` and handed back through the
  same `metadata["graph"]` channel, so entity resolution and remapping treat them identically
  to a live payload). The cost of preserving is stated rather than hidden: for those
  documents a rebuild can add and correct but **cannot remove**, until that source syncs once.
  A document whose chunks are gone is `missing_text` and left exactly as it was — rebuilding
  it into an empty graph would delete real edges over missing input. `with_triples=False` is
  the default and spends **no LLM calls** (`_sync_graph` gained `defer_triples` to bypass the
  `graph_pending` branch); `with_triples=True` re-queues one model call per document, which on
  the live 19,538-document corpus is ~12 hours, so it is opt-in everywhere and the CLI
  confirms first. Ends with `gc_orphan_entities` + `refresh_same_as_bridges` (both
  best-effort — an untidy rebuilt graph beats a failed one). Tests: `tests/test_regraph.py`
  (13, incl. the preserve guarantee verified to fail with preservation disabled, payload
  capture + backfill-without-re-embed, missing-text left alone, scoping, version stamping,
  and that the default path calls no extractor) + 4 route tests; all new catalog methods
  **verified on pgvector**, not statically reviewed.
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
  **Relation signatures (ontology-lite domain/range validation, 2026-07-30, AI_ROADMAP #21):**
  the type check and the relation check were independent, so a line whose three words were each
  in-vocabulary became a real edge even when the combination is a category error —
  `environment: prod | owns | person: bob` validated. `RELATION_SIGNATURES` declares, per relation,
  which entity types may stand on its LEFT (domain) and RIGHT (range); `signature_allows` (pure) is
  a second gate inside `parse_triples`, dropping an off-signature line exactly like an
  off-vocabulary one — never coercing it. Deliberately **permissive**: it rejects impossible
  shapes, not arguable ones, because a tight signature silently deletes true relationships (the
  failure that actually matters for a system whose claim is that it only says what it learned).
  `references` is explicitly `UNSIGNED_RELS` — it asserts co-occurrence ("mentioned alongside"),
  not a typed link, so constraining it would only invent violations.
  **Calibrated against the real 109k-edge graph, not taste (same day, during live testing):**
  the first cut was drawn around an idealised ontology and rejected **4,201 of 29,461**
  in-vocabulary edges (13.6%) — of which **43% were perfectly sensible statements** real org
  prose makes constantly (`person owns ticket`, `person works_on team` ×246,
  `team provides service`, `service part_of environment` ×328). The data taught the actual
  rule: **almost every genuine error is a *domain* error** — the subject cannot perform the
  relation (inverted `ticket works_on person` ×548 dominates, then software "working on"
  things, then places/channels acting as agents). **Ranges** only earn their keep where the
  object type is definitional (publish→topic, store→datastore, deploy→software|place).
  So subjects are constrained tightly, objects loosely, and `person` is excluded as an object
  generally (people own and work on things, not the reverse). Re-measured: **2,441 rejections
  (8.3%), effectively all real category errors** (107 residual, itself dominated by the
  genuine `service publishes_to service`). Live-verified end to end against gemma4: 13 triples
  extracted from realistic prose, **all signature-valid**, including the two shapes the first
  table would have wrongly dropped — regression-guarded by
  `test_signatures_admit_the_shapes_real_org_prose_actually_uses`. `SIGNATURE_LINES` renders the
  table into **both** extraction prompts from that same dict (same lockstep discipline as the
  ALLOWED_* lines), so a model is told the shape up front instead of having lines silently
  discarded. The deterministic extractors don't route through `parse_triples` and build their
  edges from structure, so every shape they emit conforms **by construction** — pinned by
  `test_deterministic_extractor_edges_satisfy_their_signatures` rather than given the
  lower-confidence scoring penalty the roadmap item speculated about (it could never fire).
  A new verb in `TRIPLE_RELS` must declare a signature or be listed unsigned — lockstep-tested.
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
  **Parallel file reading** (`files.read_documents_parallel`, 2026-07-22): both `files` and `git`
  read their file trees on a bounded `ThreadPoolExecutor` (disk `stat`/`read_text` release the
  GIL ⇒ real I/O parallelism) instead of one file at a time — the file-phase bottleneck on a large
  repo, and the whole cost of a re-sync (unchanged files are still read to hash them). Yields
  **in submission order** (stable progress/URIs), keeps a bounded read-ahead window (memory-safe on
  huge trees) that also **overlaps reading with the downstream embed**, and preserves pause/stop +
  `%` via the per-file `stage` callback — reads are side-effect-free, so a `SyncStopped` unwinds
  cleanly and nothing commits until the pipeline ingests (idempotent). Worker count auto-scales to
  the machine (`read_workers()`), `QJ_READ_WORKERS` overrides (1 ⇒ the old sequential path). This
  is the connector-side slice of AI_ROADMAP **S6**. Tests: `tests/test_parallel_read.py`
  (order-preserving under out-of-order completion, None-skip, stop-honored, env override).
  `files.read_file_document` now routes **office/PDF** files (`.docx/.pptx/.xlsx/.pdf`) through
  `ingest.extract` (roomier `MAX_DOC_BYTES` cap) alongside the text/code/markdown it already read,
  so the `files`/`git` connectors ingest Word/PowerPoint/Excel/PDF too (a file we can't parse is
  skipped, not fatal).
  **Auto-wire the API connector from a git URL** (`git_repo.suggest_api_connector`, 2026-07-23): a
  pure host-heuristic that maps a clone URL to the matching **API** connector that layers
  MRs/issues/pipelines + the ticket↔MR graph on top of the cloned code — `github.com`/`github.*` →
  `github` (`repo=org/name`), `gitlab.com`/`*gitlab*` → `gitlab` (`project=full/group/path` +
  `base_url` for self-managed); `None` for a plain host. Token left unset ⇒ falls back to
  `$GITHUB_TOKEN`/`$GITLAB_TOKEN`. Handles https + scp (`git@host:group/repo.git`). Wired into
  `cli.py connect`: after saving a `git` source it offers (interactive `typer.confirm`) to also create
  the API connector, or prints the ready-to-run command non-interactively. The web wizard consumes the
  same helper (planned — see the connector-forms note). Tests: `tests/test_connectors.py`
  (`suggest_api_connector` github/gitlab/enterprise/scp + None for unknown hosts).
  **Push-triggered re-sync** (2026-07-26, `Mode.PUSH` added): a push webhook to the mapped
  `branch` option (or ANY branch, when unset — the clone then just tracks the remote's own
  default, so a push there is always relevant) re-syncs within seconds instead of waiting for
  the scheduled pull. Structurally different from every other PUSH connector: a push payload
  carries commit metadata, never file contents, so there's nothing `handle_event` could turn
  into a document directly — `wants_resync(payload)` (reads the `ref` field GitHub's and
  GitLab's push events both use) returns `True` instead, and `handle_event` yields nothing; see
  the `api/hooks.py` bullet above for how the router acts on that. `pushed_branch` (pure) parses
  the ref, returning `None` for a tag push or unrecognized shape rather than guessing. Point the
  git host's push webhook at `POST /hooks/<source>` same as any other connector. Tests:
  `tests/test_connectors.py` (`pushed_branch`, `wants_resync` mapped/unmapped/tag-push),
  `tests/test_api.py` (a matching-branch push starts a real `SyncManager` job, not a
  direct-ingest call; a different-branch push is silently ignored).
  **`onedrive.py` + `msgraph.py` — OneDrive for Business / SharePoint, per user, ON DEMAND
  (2026-07-24):** the first connector authenticated as a **person** rather than with a static
  secret. `msgraph.py` owns Microsoft identity: **two** interactive sign-in flows because
  QuickJoiner runs in two places — **device code** (`qj onedrive login <name>`; no redirect URI,
  no client secret, so it works over SSH/in Docker/headless) and **authorization code + PKCE**
  (`POST /api/connectors/{name}/oauth/start` → Microsoft → `GET /api/oauth/callback`, the
  browser path). Both end in a `TokenBundle` persisted to `<workspace>/oauth/<source>.json` —
  the **filesystem, not the catalog**, because `create_connector(source, workspace)` hands a
  connector no catalog, so a file is the only store reachable from every path that builds one
  (CLI, API, scheduler, sync manager); it sits beside `browser_profile/` and `uploads/` for the
  same reason. `GraphClient` owns the access-token lifecycle and **persists the rotated refresh
  token** (Microsoft rotates on every redemption — dropping it works for the rest of the process
  then fails on the next sync), retries 401 once after a forced refresh, and honours
  **`Retry-After`** (Graph throttles hard and ignoring the header gets you throttled harder).
  `bundle_from_response` keeps the previous refresh token when a response omits one.
  `GraphAuthError` is its own type so a dead token says "sign in again" instead of surfacing a
  raw 401 — and so `is_transient_network_error` correctly classifies it as **not** worth
  retrying. **Delegated scopes only** (`scopes_for` composes least-privilege from the configured
  `access`: my_drive→`Files.Read`, shared_with_me→`Files.Read.All`, sharepoint→`Sites.Read.All`,
  always `offline_access User.Read`) — the connector can see exactly what the signed-in person
  can see, including files shared *with* them, and there is no application-permission path at
  all. **On demand, not a crawl** (user requirement): `sync()` deliberately discovers nothing —
  it re-reads the items in a per-connector **learned-item manifest**
  (`<workspace>/onedrive/<source>.json`) so scheduled syncs keep what you taught it current.
  Documents get in one way: `connector.learn(targets)` → `POST /api/connectors/{name}/onedrive/
  learn` (the `/qj learn from this onedrive document <url>` path) or `qj onedrive learn`.
  `sharing_token` (pure) encodes any pasted OneDrive/SharePoint URL as a Graph `u!` sharing token
  so `/shares/{token}/driveItem` resolves it under the caller's own permissions — that is what
  makes "paste any link you can open" work; a bare path resolves against `/me/drive/root:/`. A
  folder target expands to the readable files beneath it (bounded `MAX_LEARN_ITEMS`=250,
  reported). `learn` returns `(documents, notes)` and **every skip is a note** — the API, CLI and
  UI all show what was NOT learned rather than implying success. `item_ref` follows `remoteItem`
  (shared entries and search hits are shortcuts; using their own ids 404s on download — the
  classic first bug with those endpoints), and `item_document` uses the item's **webUrl** as the
  uri so citations render as clickable links back into OneDrive. LIVE tools
  (`onedrive_search` over Microsoft Search, `onedrive_read_file`) are **GET-only** per the plan-09
  rule and say so in their descriptions — finding a file never ingests it, which also stops a
  model deciding on its own to put someone's OneDrive into communal memory. PUSH: `handle_event`
  refreshes **only** items already in the manifest, so a webhook can never widen what is ingested
  (Graph change notifications also need a publicly reachable HTTPS callback, so PUSH is
  deployment-gated). `test()` reports "not signed in yet" as **ok** on purpose — the token is
  keyed by source_id so it cannot exist before the connector does, and a failing test blocks
  creation; the two would deadlock. New base-class hook **`Connector.on_deleted()`** (default
  no-op) releases workspace-side state on delete — OneDrive drops its refresh token and manifest,
  because a live token left on disk after its connector is gone is still redeemable.
  ⚠ **Communal-memory caveat, stated in three places** (`test()`, the connect form, and before
  every learn): ingested content joins QuickJoiner's ONE communal memory, so a privately-shared
  file becomes answerable and citable for every workspace user. Per-user knowledge scopes are
  **PRIORITIES #2** (raised from #14 by this connector). Tests: `tests/test_onedrive.py` (34 —
  pure converters, PKCE/token lifecycle, MockTransport round trips, on-demand learn, manifest,
  folder expansion, webhook narrowing, tool consolidation) + 6 API tests in `test_api.py`.
  `uploads.py` — **the rolling Uploads connector** (2026-07-22): one permanent, continuously-growing
  document drop-box instead of a connector-per-file. Everything a user adds ad-hoc — a chat
  drag-drop, a `/qj` "ingest this file", the `POST /api/uploads[/local]` endpoints — lands in one
  managed folder `<workspace>/uploads/` and ingests into a single source (`uploads:uploads`),
  accepting anything `ingest.extract` understands. A seeded singleton like the control connector
  (`_ensure_uploads_connector`, ownerless commons): auto-created, **un-deletable / un-renamable /
  never user-duplicated** (409 guards `_guard_not_uploads` + a create-type block), but — unlike the
  control connector — it DOES produce documents and IS syncable + cleanable. It subclasses
  `FilesConnector` with `_target()` → the managed folder, so a re-sync re-scans and picks up
  new/changed files idempotently (hash dedupe). `save_upload(workspace, filename, data)` writes an
  upload under a safe, path-traversal-proof, collision-aware name (identical bytes ⇒ same path/
  idempotent; different content sharing a name ⇒ short content-hash suffix). Because the endpoints
  read the saved file with the SAME `read_file_document`, an upload and a later folder sync produce
  the same doc uri/id — no duplicates. `is_uploads_source()` is what the API guards check. Tests:
  `tests/test_uploads.py` (folder read, save collisions/traversal, seeded+permanent, endpoint
  ingest + idempotent re-sync, unreadable-file reporting).
  `jira.py` pulls issues via JQL (`projects`/custom `jql` options compose with a watermark
  `updated >= since` clause for incremental syncs; the first sync has no watermark, so it's
  effectively "most recently updated, capped at `MAX_ISSUES`=1000"), paginated with a prefetch
  overlap (`prefetch_pages`) like the other connectors. **Hierarchy + related-issue graph**
  (`issue_document`, extended 2026-07-26 for parity with the ADO connector's dev-link/hierarchy
  work): `ticket --part_of--> project` always; `ticket --part_of--> ticket:parent` when
  `fields.parent` is set (Jira's own Epic-link/subtask-parent field); and, new,
  `ticket --related_to--> ticket` from `fields.issuelinks` (`_linked_issue_keys`, pure) —
  flattened to one relation regardless of link type (Relates/Blocks/Duplicates/…), matching how
  the ADO connector treats `System.LinkTypes.Related` as a single undirected `related_to` rather
  than a typed taxonomy. **Walk-up for missing parents** (`sync()`): the JQL/incremental pull only
  ever returns issues that were THEMSELVES recently updated, so an Epic that hasn't been touched
  would never reach memory at all — the exact gap ADO's hierarchy walk-up closed for TFS. After
  each page, any `parent` key not yet seen is queued; a bounded (`MAX_PARENT_DEPTH`=8) follow-up
  pass fetches them via a batched `key in (...)` JQL clause (not one call per id) and recurses on
  THEIR parents. Jira's real hierarchy is normally just 2 levels (Epic > Story/Task, no
  grandparents), so the bound is generous headroom, not an expected depth. **Display metadata**
  (`Document.metadata["display"]`, same shape and same document-browser consumer as ADO's): `id`/
  `parent_id` are Jira **keys** (strings like `"PROJ-123"`, not numbers) — deliberately so, since
  the frontend tree (`DocumentsModal.tsx`) keys everything by `String(id)` uniformly so one
  implementation serves both connectors. `team`/`sprint` are intentionally left blank: Jira's
  board/sprint data lives in a separate Agile REST API (`/rest/agile/1.0/...`) this connector
  doesn't speak, a deliberately deferred, genuinely-different-platform gap (not just an unported
  feature) — the tree/filter UI already renders fine with those fields absent. `assigned_to`/
  `tags` map directly from `fields.assignee`/`fields.labels` (Jira's `labels` is already a real
  array, unlike ADO's semicolon-joined `System.Tags` string). **`jira_get_issue` live tool**
  (new, mirrors `ado_get_work_item`'s role): `jira_search` only ever returns
  summary/status/assignee for a LIST of matches — there was no way to pull one issue's full
  description, linked issues, or recent comments live. Registered alongside `jira_search` in
  `tools()`. Tests in `tests/test_connectors.py`: `_linked_issue_keys`/related_to edges, display
  metadata shape, the walk-up (monkeypatched `get_json`, asserts the batched `key in (...)` call
  actually fires and the missing parent is yielded), `jira_get_issue` (full detail + links +
  comments). **Document-browser tree generalized to both connectors** (`DocumentsModal.tsx`):
  `AdoMeta`/`adoMetaOf` renamed to `WorkItemMeta`/`workItemMetaOf`, id/parent_id typed
  `string | number` throughout `buildWorkItemTree`/`collectAncestorIds` (Map/Set keys are always
  `String(id)`), and the render gate is now `WORK_ITEM_TREE_SOURCE_TYPES = {"azure_devops",
  "jira"}` instead of a single ADO check — so a Jira source gets the same Epic/Story/Task tree,
  iteration/assignee/tag filters, and ongoing-then-completed sort as ADO, with zero UI-specific
  Jira code (this was the point of keeping the tree logic generic from the start). This also
  incidentally fixes a pre-existing Jira document-browser bug: a Jira issue's uri
  (`.../browse/PROJ-123`) has no trailing path structure, so the generic folder grouping
  (`groupByFolder`) gave every issue its own one-item "folder" — the same class of bug Confluence
  had before its space-grouping fix — which the tree view replaces entirely for Jira sources.
  **Deliberately not done here** (flagged, not silently skipped): sprint/board windowing (the
  genuinely-different-platform item above) and an unrelated bug noticed while comparing —
  the ADO connector's OWN webhook handler (`handle_event`) calls `work_item_document` with no
  `repo_names`/`team`, so a push-ingested work item gets no dev-link graph and a blank team; not
  fixed in this pass since it's an ADO-side gap, not a Jira-parity one.
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
  **Work-item Development links → ticket↔code↔GitLab graph** (`dev_link_graph`, 2026-07-23): each
  work item's `relations` (its "Development" section) carry `vstfs:///Git/{Ref|Commit|PullRequestId}/…`
  **artifact links** encoding the repo GUID + branch/commit/PR it was implemented in. The batch
  work-item fetch now passes **`$expand=relations`** (same call, no extra round-trip), a single
  `_git_repo_names` call maps repo GUID→name, and `_decode_git_artifact` (pure) + `dev_link_graph`
  (pure) emit deterministic edges — evidence = the work item, TFS *stated* the link, not inferred:
  `ticket:#N --implemented_in--> repo:<name>` (branch/commit/PR), plus for branch (GB) refs
  `ticket:#N --on_branch--> branch:<repo>/<name>` + `branch --belongs_to--> repo` (GT tags → repo edge
  only, no branch node). Entities key by **name**, so name parity with the GitLab connector's
  `repo:<name>` links a TFS ticket through to the **GitLab repo it was implemented in** regardless of
  GitLab's differing group nesting (the user's TFS↔GitLab mirroring: same repo + branch name). A link
  whose repo GUID can't be named is **skipped, never guessed**; best-effort throughout (a repos-list
  failure ⇒ no dev-link edges, never a failed sync). New graph vocab: entity type `branch`, rels
  `implemented_in`/`on_branch`/`belongs_to` (connector-emitted metadata, not the LLM triple vocab —
  see `docs/KNOWLEDGE_GRAPH.md`). **Needs an ADO re-sync** to populate. Content hyperlinks in
  descriptions/comments/ACs are still NOT turned into edges (that's Phase 2 — designed, not built).
  Tests in `tests/test_connectors.py` (on-prem URL/search-host/api/verify + SaaS guard;
  `select_recent_iterations` future-exclusion; `build_map_document` builds-edges; `builds_document`
  source-branch; 4-tool name list; `dev_link_graph` ref/commit/PR edges + unknown-GUID skip +
  `work_item_document` graph attach).
  **Work-item HIERARCHY (Epic→Feature→Story/Bug→Task) + Related links** (`hierarchy_graph`,
  2026-07-26, AI_ROADMAP #26 — the piece `dev_link_graph` above deliberately doesn't touch, since
  it only reads `ArtifactLink` relations): `hierarchy_graph(item)` reads the SAME already-expanded
  `relations` array for `System.LinkTypes.Hierarchy-Reverse` (this item's parent) and
  `System.LinkTypes.Related`, emitting `ticket:#N --part_of--> ticket:#parent` (matching the Jira
  connector's own `part_of` convention so both read the same way to the agent) and
  `ticket:#N --related_to--> ticket:#M`. `_merge_graphs` combines this with `dev_link_graph`'s
  output into one `metadata["graph"]` per document — entity type stays uniformly `"ticket"` (Epic
  vs. Feature vs. Story vs. Task is a work-item-TYPE distinction, not a graph-vocabulary one).
  **Walks the hierarchy both UP and DOWN** (`sync()`, `_next_hierarchy_ids`, bounded
  `MAX_HIERARCHY_DEPTH=8`, chunked in `WORK_ITEM_BATCH`-sized `$expand=relations` calls — every
  fetched item's relations already include BOTH directions, so this costs no extra API call
  beyond fetching the ids it turns up): a parent Feature/Epic carries no sprint iteration of its
  own, so the existing team/iteration pull would never return it — after each team's batch
  yields, unresolved Hierarchy-Reverse **parent** ids are collected and fetched (and THEIR
  parents, recursively, bounded) so Epics/Features reach memory even from outside any sprint
  window. **DOWN, added 2026-07-26 after live re-sync verification** (`_next_hierarchy_ids`):
  for an Epic or Feature specifically (never a Story/Bug/Task — that would reopen the flat-300k-
  item problem the sprint window exists to avoid), its Hierarchy-Forward **children** are ALSO
  collected and fetched. Without this a Feature/Epic was only ever discovered via one descendant
  happening to still be in-window, and even then showed only THAT one child — verified live
  against the real AppRiver workspace: a "CRSB Phase 2 - Tech Debt" Feature was entirely absent
  (none of its children were recent enough for anything to find it), and a sibling "Provisioning"
  Feature that WAS discovered showed only 3 of its ~10 real children. Both directions share one
  bounded walk loop and one `seen` set. **Per-document display metadata** (`work_item_document(..., team=)`,
  `Document.metadata["display"]` — deliberately separate from `["graph"]`, which becomes graph
  rows, not stored raw): `work_item_type`/`state`/`team`/`sprint`/`changed_date`/`closed_date`/
  `parent_id`, persisted via a new `documents.metadata_json` column (`catalog.upsert_document`
  gained the param; `catalog.update_document_metadata` is a standalone cheap-UPDATE backfill path
  wired into `pipeline._ingest_one`'s existing stale-graph-refresh branch, riding the SAME
  `GRAPH_EXTRACTOR_VERSION` bump — v2→**v3** — so team/sprint/state self-heal onto
  already-ingested-but-unchanged work items on the next ordinary sync, exactly like the edges do,
  with no re-embed). `team` is the team whose sprint pull surfaced the item; blank (never guessed)
  for anything reached only by walking the hierarchy (up or down), which can span many teams.
  **Document browser tree view** (`frontend/src/components/DocumentsModal.tsx`): for
  `sourceType === "azure_devops"`, `buildWorkItemTree` builds the Epic/Feature/Story/Task tree
  ENTIRELY client-side from each document's own `metadata.id`/`metadata.parent_id` — not by
  querying graph edges (those exist for the agent's multi-hop reasoning; the tree is a simpler,
  separate read of the same hierarchy for display). A doc whose `parent_id` doesn't resolve to
  another doc in the source becomes a root — covers a real Epic and an orphan (parent outside the
  walk-up depth bound, or in another project) without special-casing either; orphans are flagged
  ("parent not ingested"), never silently shown as a top-level Epic. **Sort** (`sortSiblings`,
  applied recursively at every level): not-yet-completed siblings first (most recently updated
  first — `changed_date` desc), then completed siblings (most recently completed first —
  `closed_date` desc, falling back to `changed_date`). "Completed" is a state-NAME heuristic
  (Closed/Done/Resolved/Removed/Completed, case-insensitive) — no extra ADO API call, covers
  Agile/Scrum/CMMI/Basic; a heavily customized process template with unusual state names could
  misclassify an item for ordering purposes only, never for grounding. Search-filtering in tree
  mode keeps every ancestor of a match (`collectAncestorIds`) so a matched deep child stays in
  visible context — a pruned tree, not a flat list. Every other connector type is unaffected
  (`metadata` is `{}` unless a connector sets it; the folder/Confluence-space grouping is
  untouched). Tests: `tests/test_connectors.py` (`hierarchy_graph`/`_merge_graphs`/
  `_workitem_id_from_url`, team defaulting, merged dev-link+hierarchy graph),
  `tests/test_catalog.py` (`metadata_json` round-trip, `update_document_metadata` backfill),
  `tests/test_pipeline.py` (stale-graph refresh also backfills metadata without re-embed),
  `tests/test_chat_attachments.py` (documents endpoint carries `metadata`). Frontend: no dedicated
  test suite per this repo's practice — `buildWorkItemTree`/`sortSiblings`/`isCompleted` kept as
  small, readable, exported pure functions for exactly that reason. **Live-verification pending a
  real ADO re-sync** — the walk-up/backfill logic only fires on a genuine sync, which needs the
  user's connector to actually run one; not yet observed against a real hierarchy in the browser.
  `octopus.py` **paginates** every list endpoint via `_paged` (follows `Links["Page.Next"]`) — a
  space with >100 projects previously truncated at the `take=100` first page. Pull is a full refresh
  (idempotent via hash dedupe); opt-in `incremental=true` fetches per-project releases only for
  projects with an Octopus event since the last sync (`_changed_project_ids` over `/events?from=`),
  while the paginated project list + the deployment dashboard (which carries the
  `service→deploys→environment` edges) always refresh — falls back to a full pull on first sync or
  any events error. True zero-poll freshness is PUSH via an Octopus Subscription → `/hooks/<source>`.
  Tests in `tests/test_phase4_connectors.py` (paging walks Page.Next, incremental skips unchanged,
  no-watermark full fallback).
  `gitlab.py` ingests MRs/issues/pipelines/wiki via the REST API (the code itself is indexed by the
  separate `git` clone connector). **Comprehensive READ-ONLY live tools (plan 09, 2026-07-23) — the
  ENUMERATION + CURRENT-STATE path** that learned memory can't serve (memory returns top-k *similar*
  chunks, never "all matching a filter", and a "list all open MRs" query often scores *below* `min_score`
  and refuses). GitLab's 19-tool family: MRs (`list`/`get`/`reviews`), `list_issues`, pipelines
  (`pipeline_status`/`pipeline_jobs`/`job_log`), `list_commits`/`compare`/`list_branches`,
  `project_info`, `search_code`/`get_file`, and P2: `list_releases`/`list_milestones`/`list_members`/
  `list_contributors`/`list_environments`/`list_tree`. GitHub has the **parallel** 18-tool family
  (PRs `list`/`get`/`reviews`, issues, `workflow_runs`/`run_jobs`/`job_log`, commits/compare/branches,
  `repo_info`, search/get_file, releases/milestones/contributors/environments/tree); ADO added
  `ado_get_work_item` (+ dev-links/acceptance-criteria), `ado_build_details`, `ado_build_log`,
  `ado_list_repos`/`ado_list_pipelines`/`ado_list_commits`/`ado_test_results`.
  All GET-only — **no mutating call**. Built via `connectors/live_tools.py` (`read_tool` + `bounded`
  output cap + `make_resolver`). **Consolidated per TYPE, not per instance (Phase 0):** the tools are a
  `@classmethod type_tools(connectors)` returning ONE set with a `project`/`repo` **selector** arg
  (resolved by `make_resolver`; optional when a single connector of that type is configured, a
  "specify which project (one of: …)" hint when ambiguous), so AppRiver's 3 GitLab projects don't
  multiply into 3×13 tools. `app.py::connector_tools` groups sources by type and calls `type_tools` when
  present (else per-instance `tools()`, which now just delegates to `type_tools([self])`). The
  enumeration/current-state → live-tool split is taught in `agent/prompts.py`. **Group/org-scoping
  (Phase 3, 2026-07-24):** a GitLab connector configured with a **`group`** (e.g. `zix`) instead of a
  single `project` goes org-wide — its selector resolves ANY repo in the group **by name** via
  `GET /groups/{g}/projects?search=…&include_subgroups=true` (`_resolve_project_in_group`, cached;
  ranks exact/suffix then shortest so "connector" → `appriver.connector` not `…azure.connector`), and
  `list_merge_requests`/`list_issues` with no repo named enumerate **group-wide**
  (`/groups/{g}/merge_requests`). INGESTION stays explicit: a group connector ingests only the paths in
  its **`projects`** list (`sync` loops `_sync_project` per path; empty ⇒ tools-only), so one
  `group=zix` connector replaces one-per-repo without auto-ingesting hundreds. `project` is no longer a
  required FORM_SPECS field (`group`/`projects` added). Tests:
  `tests/test_connectors.py` (consolidation+selector, group resolution + group-wide enum, GitLab/GitHub
  families, ADO name set).
  **Repo/branch/MR graph** (`mr_graph`, 2026-07-23): each MR emits
  `merge_request:<repo>/!<iid>` + `branch:<repo>/<src>` + `repo:<repo>` entities and
  `mr --from_branch--> branch`, `mr --in_repo--> repo`, `branch --belongs_to--> repo` edges. The
  `repo`/`branch` entities are keyed by the repo's own **name** (`_repo_name` prefers the GitLab
  project's `name` field, group-independent), so they **merge with the TFS dev-link side** by name
  parity — `ticket --on_branch--> branch:<repo>/<x> <--from_branch-- merge_request:<repo>/!N` is the
  join that answers **"what's the MR for this TFS issue?"** (new entity type `merge_request`; rels
  `from_branch`/`in_repo`). **Org-specific integration rules (all OPT-IN, off by default — not every org
  shares these conventions):** (1) **`ticket_in_branch`** (or custom `ticket_pattern`, regex group 1 =
  id): a TFS work-item id embedded in a branch/MR name yields a DIRECT `merge_request --implements-->
  ticket:#<id>` (+ `branch --for_ticket--> ticket:#<id>`) edge — the strongest join (keys `ticket:#<id>`
  exactly as the ADO connector does). Default pattern = first run of **4+ digits, searched anywhere**
  (not anchored); live-verified against AppRiver whose branches are `type/team/<ticket>-slug`
  (`feature/acadia/321135-ceb-product-card` → #321135), so the id is NOT the first segment. (2)
  **`tfs_sync_stage`** — see below. **Branch identity bridge** (`ingest/bridges.py`): branch entities are
  same-type so the cross-type bridge pass skips them; a second pass bridges `branch:<repoA>/<x>` ⇔
  `branch:<repoB>/<x>` when the repo parts are the **same repo family** (union-find over repo names +
  aliases), so the ticket↔MR join survives a repo spelled differently on each side (`connector` vs
  `appriver.connector`); the name-parity case needs no bridge (ids already equal, as AppRiver's are).
  Guard: `MAX_BRANCH_GROUP`=8. **Live-verified 2026-07-23** against real AppRiver GitLab: connection OK,
  `_repo_name` → `AppRiver.Connector` (== TFS), MR edges + ticket extraction correct on 5 real MRs.
  **Per-branch "synced to TFS" capture** (`branch_sync_document` +
  `_find_tfs_sync`, **opt-in** via the `tfs_sync_stage` option = the mirror job/stage name substring;
  `tfs_sync_lookback_days` default 15): for each recent MR source branch, walks that branch's pipelines
  newest-first within the look-back and records the first with a **successful** job matching
  `tfs_sync_stage` — the GitLab→TFS mirror that underpins the branch parity — as one doc carrying
  `branch --synced_to_tfs--> repo` edges (detail = pipeline id + date). Bounded
  (`TFS_SYNC_MAX_BRANCHES`=60, `…_PIPELINES_PER_BRANCH`=10) + `_checkpoint`ed + best-effort (any API
  error ⇒ skip, never fails the sync). **Needs a GitLab connector + sync** to populate; the token
  falls back to `$GITLAB_TOKEN` when the field is blank (`resolve_secret`). Tests in
  `tests/test_connectors.py` (`mr_graph` edges; the TFS↔GitLab **branch-id join**; `mr_document` graph
  attach; `branch_sync_document`). *Phase 2 still open:* content-hyperlink harvesting (Confluence/ADO).
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
  **Crawl identity: canonical URLs, a same-host floor, content dedupe, honest titles
  (2026-07-30, user-reported "numerous homepage entries").** Measured against the live
  `plumber.appriver.corp` crawl before changing anything, which separated the real defects from
  the imagined ones. **Already correct, confirmed not assumed:** every one of the 200 ingested
  documents was on the start host (zero TFS links followed — `extract_links`' prefix filter plus
  `default_prefixes` scoping to the start URL's folder), and no URL was fetched twice in a pass
  (`visited`/`enqueued`). **Actually wrong:** (1) dedupe compared *raw URL strings*, so spellings
  of one page (host case, default port, `//`, parameter ORDER, a `utm_*` tracking param) each
  became their own document — a doc_id is `sha256(source_id|uri)`. `canonical_url` (pure) fixes
  that, and deliberately does **not** drop parameters for looking like filters: `?team=30` and
  `?team=41` are different pages and merging them loses real content, the worse of the two errors.
  Escaping is preserved as `%20`, not rewritten to `+`, so the stored uri is the one a user could
  paste. (2) Two genuinely different URLs rendering byte-identical content still became two
  documents — 23 of that crawl's 200-page budget went on the dashboard's own `?tag=`/`?team=`
  filter facets. The crawl now hashes each page's extracted text and yields a repeat once;
  **exact equality only**, since anything looser starts discarding pages that merely resemble
  each other. (3) **Titles are not identifiers**: the app served ONE static `<title>`
  ("Home page - AppRiver.ContinuousDelivery") for all 200 pages, so the document browser showed
  200 identical rows and the contextual-chunking breadcrumb learned nothing from any of them.
  `page_title` prefers an `<h1>` **from the post-`DROP_TAGS` content region** (so a brand `<h1>`
  in a `<header>` is already gone) and falls back to `<title>` then the URL — weakly dominant: a
  site that repeats its name in the body `<h1>` is no worse off than the `<title>` it replaces.
  (4) The crawl was **silent about what it left out** — it hit `max_pages` exactly and looked
  complete; `_crawl` now reports both the duplicate count and a truncation warning naming how many
  links were still queued. Also added: `same_host_only` (option, default true, in `FORM_SPECS`) as
  a hard floor **independent of** `allow_prefixes` — the prefix list is user-editable and one
  over-broad entry would let the crawl wander into a system it isn't the connector for. NB titles
  change only on **re-ingest**: the doc hash is over text alone, so already-ingested pages keep
  their old titles until a clean re-sync. Tests: `tests/test_scraper.py` (canonicalization
  collapse/preserve, host floor vs an over-broad prefix list, content dedupe, `<h1>` preference +
  the brand-heading false positive).
  **Server error pages are not content (`looks_like_error_page`, 2026-08-04, AI #31).**
  An ASP.NET Core app renders `Error.cshtml` **in place with a 200**, so nothing upstream
  rejects it and the page lands as a real, answerable, citable document: measured **363 of
  728** documents in the live `web_scrape` corpus were the identical *"An error occurred
  while processing your request"* page. Content dedupe collapses them to one on the next
  clean re-sync, but one junk document is still one too many, and a crawl that indexes its
  own failures overstates coverage. Detection is deliberately **stricter than
  `looks_like_login`** next door, because that one only ever *reports* while this one
  **skips** and a false positive silently drops a real page: it needs a framework
  boilerplate marker (`_ERROR_MARKERS` — ASP.NET/IIS/nginx/Apache phrasing a genuine page
  has no reason to contain verbatim) **AND** brevity (`_ERROR_MAX_CHARS` 1500), so a real
  runbook or API error-code reference — which has substance — is not mistaken for one. The
  crawl still **follows a skipped page's links** (the page failed, the site did not) and
  reports the count plus the first offending URL via `_stage`, per the no-silent-caps rule:
  an over-eager rule then shows up as a suspicious number rather than as a thin corpus
  nobody questions. Pure and unit-tested, including both false-positive guards.
  **Scraped pages preserve tables as markdown rows.** `page_document` does its own
  extraction (it has already stripped `DROP_TAGS` and picked a content region), so it calls
  `ingest.extract.render_html_table` on leaf tables directly — without that call the whole
  table-fidelity fix would reach every source *except* the scraped pages that motivated it.
  Same two shape rules as the extractor: leaf tables only (a wrapper is page layout), and
  `get_text()` is never let near a data table, since it drops a blank cell and shifts the
  rest of the row into the wrong columns.
  Browser hardening lives in `browser/session.py` (`DESKTOP_UA`, `_CONTEXT_OPTS`, `_STEALTH_JS`
  masking `navigator.webdriver`) — applied to both `qj browser login` and headless fetch.
  **Credentialed sites: session-cookie capture + auth-wall detection (2026-07-30, found live
  against an internal OIDC app).** A persistent profile only retains cookies Chromium writes to
  **disk** — i.e. those carrying an explicit `Expires`. The cookie that actually authenticates
  you usually has none: ASP.NET Core's `.AspNetCore.Cookies`, and any `IsPersistent=false`
  sign-in, are **session cookies** held in memory and discarded when the window closes. So a
  user could sign in perfectly and end up with a profile containing only OIDC
  `Correlation`/`Nonce` handshake crumbs (those *do* carry Expires) — and every later fetch
  landed back on the login page, was dropped by `page_document`'s 80-char floor, and the sync
  reported a bare **"0 documents"**, indistinguishable from an empty site. Three fixes, all in
  `session.py`: (1) `login()` snapshots `context.storage_state()` **while the window is still
  open** (that export includes session cookies as `expires: -1`) to `<workspace>/browser_state.json`
  every `_SNAPSHOT_SECONDS`, and `browser_session()` re-injects them via `add_cookies`
  (per-cookie salvage on failure; the profile still does the heavy lifting). (2)
  `looks_like_login(final_url, requested_url, text, title)` (**pure, tested**) — two independent
  signals, either sufficient: the browser ended on a **different host** than requested (the SSO
  redirect), or the page reads like a sign-in form (≥2 markers AND < 2000 chars, so a genuine
  page *about* auth isn't flagged). (3) `verify_session(workspace, url)` fetches through the
  saved session and reports content-vs-auth-wall. Wired in: `qj browser login` prints
  "✓ signed in — you can close the window now" the moment the round-trip lands and **verifies
  after the window closes** (exit 1 if not, with the Windows-integrated-auth tip);
  `qj browser status [url]` lists the hosts with saved session cookies and optionally verifies;
  `WebScrapeConnector.test()` with `use_browser=true` now **actually fetches the start URL**
  instead of merely checking a profile directory exists (it used to green-light a connector that
  could not fetch a single page); and `_crawl_via_browser` counts auth-wall pages and reports
  `⚠ N page(s) returned a sign-in page…` via `_stage`, so a zero-document sync says *why*.
  `_is_handshake` keeps OIDC crumbs from being mistaken for a real session. Tests:
  `tests/test_scraper.py` (login detection incl. both false-positive guards, state round-trip
  preserving `expires: -1`, handshake-cookie discrimination).
  **Sign-in from the web UI (`login_jobs.py`, same day):** `qj browser login` is a blocking CLI
  flow, so the UI drives the same thing as a background job — `POST /api/connectors/{name}/
  browser/login` starts it, `GET …/browser/session` reports state. Two deliberate constraints:
  the window's URL comes from the **connector's own `start_urls`**, never the request body (this
  route must not become "make the server open an arbitrary page"), and (superseded 2026-07-30,
  see below) `display_hint()` used to refuse up front on a headless host. One login at a time per
  connector. Frontend: `BrowserSignInPanel` on the connector plate (rendered only for
  `web_scrape` + a truthy `use_browser`, so a public crawl shows nothing), polling `verify=false`
  while a login runs and running one real verification when it ends; plus a **Sign in to this
  site** affordance in `SyncLogModal` when a finished run's log carries the auth-wall line, since
  that is where the user is looking when a sync mysteriously finds nothing. `web_scrape` gained a
  `next_step` in FORM_SPECS. Tests: `tests/test_api.py` (session reports fetch-derived signed-in
  state, login start uses the connector's configured URL, non-scrape connector → 400).
  Browser-verified against the real gated site.
  **Remote sign-in for headless hosts — Docker/cloud (2026-07-30):** the constraint above — "the
  window opens on the machine running the server" — makes the whole feature useless the moment
  QuickJoiner runs somewhere with no display at all, which is exactly the Docker/cloud case (the
  user's own question: "this works locally — what happens when I deploy this on cloud?").
  `login_jobs.login_mode() -> "local"|"remote"|"unavailable"` replaces the old binary
  `display_hint()` check: a real display → `"local"` (the flow above, byte-for-byte unchanged);
  no display but Playwright importable → `"remote"`, the new case — `display_hint()` now returns
  `None` here too, so a headless host is no longer a dead end; no display AND the `browser` extra
  not installed → `"unavailable"` (still refuses). **`session.login_remote()`** is the remote
  counterpart to `login()`: launches Chromium in ordinary `headless=True` mode (no X server
  needed at all — this is what made Xvfb+VNC unnecessary, see below) and streams the login page
  as **polled JPEG frames** (`page.screenshot(type="jpeg")` every `_FRAME_INTERVAL_SECONDS`=0.2)
  instead of putting a window on screen, so a person finishes the SSO/MFA flow from a browser tab
  that isn't this process's own display.
  **Polling, not CDP screencast (corrected 2026-07-30 after live testing):**
  the first cut used CDP's event-driven `Page.startScreencast` — efficient
  in principle since it only pushes on a compositor repaint — and produced **zero frames** against
  a real page. `startScreencast` only fires on a *subsequent* repaint, and the sequence here is
  `goto()` completes → stream attaches → the page is idle and never repaints again, so nothing
  was ever going to arrive. `page.screenshot()` always returns the current frame on demand at
  ~75ms for a full 1280×800 capture, cheap enough to poll and simpler than a CDP session (no
  session/ack handshake, no stop/restart across navigations). Runs
  entirely on the thread that owns the Playwright sync objects (not thread-safe across threads),
  draining a caller-supplied `input_queue` each tick through a **fixed-whitelist**
  `_apply_input(page, event)` (mousemove/down/up, wheel, keydown/up, insert-text — unknown/
  malformed events are swallowed, never raised, since a bad event must not kill a sign-in the
  caller can't retry) and re-targeting capture+input to the newest page on `context.on("page",
  …)` (best-effort popup handling — a separate "Sign in with Google"-style popup window is only
  partially covered by tracking the newest page, not a guarantee). Ends when a `capture_event` is
  set (the UI's **"Done — capture session"**) or after `idle_timeout_s` (900s default) with no
  input — unlike a local window there is no OS-level "closed" signal for an abandoned remote tab.
  Shares the exact same session-cookie snapshot loop as `login()` via a factored-out `_finish()`
  tail, so a remote sign-in is captured exactly as faithfully as a local one — this was a hard
  requirement, since the whole point of the snapshot loop (see the module docstring above) is not
  losing session cookies, and the remote path must not regress that. `login_jobs.py` gained a
  `RemoteSession` registry (`frame_subscribers`, `input_queue`, `capture_event`) parallel to
  `_jobs`, one per connector, alive only while its job runs; `subscribe_frames`/`push_input`/
  `signal_done` are its API. New routes: `GET .../browser/session/frames` (SSE — a `meta` event
  announcing the actual frame size first so the frontend never hardcodes a viewport, then `frame`
  events, then `done`), `POST .../browser/session/input` (one whitelisted event), `POST
  .../browser/session/done`. All three gated at the same `connectors:write` + `can_manage` tier
  as starting the login itself, not mere read access — the stream can show, and the input channel
  can type, credentials for whatever site the connector points at. `browser_session_status` gained
  an additive `remote_capable` field (`login_mode() == "remote"`, independent of any running job)
  so the frontend knows up front which UI a "Sign in" click will open. **Chosen over Xvfb +
  x11vnc + noVNC/websockify** (the literal ask) after presenting both trade-offs to the user: CDP
  screencast needs zero new Docker packages (headless Chromium already covers it — the existing
  `WITH_BROWSER=1` build arg is untouched, no Docker/compose change at all) and zero new Python
  deps, and reuses the SSE pattern already proven for live sync logs (`streamGetSSE`) plus the
  existing bearer-token auth with no second port to secure; Xvfb+noVNC would have needed several
  new apt packages, a second in-container process to supervise alongside `qj serve`, and a
  VNC-over-websocket bridge to protect. Frontend: `RemoteBrowserModal.tsx` — a `<canvas>` painted
  from decoded JPEG frames (`createImageBitmap`), pointer/wheel/keyboard handlers mapped through
  the canvas's native-vs-displayed size ratio and coalesced (~30ms) into the same input events the
  backend whitelists, footer **"Done — capture session"** / "Run in background" (same
  viewer-not-a-leash semantics as `SyncLogModal` — closing only detaches, the sign-in keeps
  running server-side); `BrowserSignInPanel` branches on the started job's `mode` to open it
  instead of the local polling flow, unchanged for `mode: "local"`.
  **Two UI defects found in live browser testing (2026-07-30, both fixed):** (1) the modal
  rendered *confined to the settings drawer* instead of covering the screen — `SettingsDrawer`'s
  panel carries `transition-transform` for its slide animation, and **any** CSS transform on an
  ancestor (even `translate-x-0`) creates a new containing block for position-fixed
  descendants, so the overlay was positioned against the drawer rather than the viewport. Fixed
  with `createPortal` to `document.body` — the same escape `SyncLogModal` gets for free by being
  mounted at the top level in `App.tsx`; the panel is 80vw × 80vh with the canvas
  flex-filling it. (2) A detached sign-in was **stranded**: "Run in background" closes the viewer
  (deliberately — it's a viewer, not a leash), but nothing re-opened it, so a running remote job
  showed a disabled "Waiting…" button forever. The button now reads **"Open sign-in view"** and
  re-attaches whenever `login.mode === "remote"` and the job is active, and the plate polls while
  any job is active (not only one it started itself, so a page reload doesn't strand it either).
  **Live-verified end to end against a real public site (x.com/login) in the headless container**,
  which isolates the streaming path from any single site's quirks: 43 frames captured over 12s
  (8KB → 44KB as the JS-driven login modal rendered), and a synthesized click + `insert-text`
  landed in the real "Email or username" field — proven by x.com's *own* JavaScript then enabling
  its "Continue" button, i.e. the page genuinely processed the events rather than merely having
  pixels drawn. Tests:
  `tests/test_browser_login.py` (pure — `login_mode()`'s three outcomes, `_apply_input`'s
  whitelist + swallow-don't-raise behavior, the `RemoteSession` registry round trip, `start()`
  wiring the right queue/event into `login_remote` and tearing the session down on completion) +
  route-level guards in `tests/test_api.py` (`remote_capable`, frames SSE body via a pre-filled
  fake queue, input/done 404-no-session and 409-if-local-mode branches). RBAC: the 3 new routes
  added to `rbac.py`'s route-capability map at `connectors:write` (the coverage lockstep test
  catches an unmapped route). **Still unverified: a real credential-gated SSO/MFA sign-in
  end-to-end** — the streaming and input paths are proven against x.com above, but no real
  protected corpus has been signed into and synced through this path yet.
  **Internal-CA sites need `verify_tls=false` (2026-07-30, found live).** The first real target
  was an internal site whose certificate chains to a private corporate CA, and every navigation
  died with `ERR_CERT_AUTHORITY_INVALID` — including the sign-in view, which then streamed
  Chrome's own certificate interstitial and read as "the remote view is blank/broken". The
  `web_scrape` connector gained a **`verify_tls`** option (mirroring the ADO connector's existing
  one), surfaced in `FORM_SPECS`; `scraper._ignore_https_errors()` is its inverse and threads
  through `session._context_opts(ignore_https_errors)` into `login()`, `login_remote()`,
  `browser_session()` and `verify_session()`, plus `login_jobs.start()` and both API routes, and
  a `--insecure` flag on `qj browser login` / `qj browser status`. Opt-in per connector, never a
  default — silently trusting any certificate would defeat TLS for every site QuickJoiner
  touches. `verify_session` also now **names a certificate failure specifically** instead of
  reporting it as a failed sign-in: the old message told the user to sign in again, which sends
  them round a loop that cannot possibly succeed. `verify_tls` reaches the **plain-HTTP client
  too** (`_get_client(verify=…)`), not just the browser paths — robots.txt is fetched through
  that client, so an internal-CA host otherwise burned the full retry/backoff budget on every
  host before the crawl could start.
  **Input pipeline rebuilt (2026-07-30, third live round — reported as "can type the username
  but not the password; paste, Enter, Delete, Backspace and Sign In all do nothing").** The
  backend was never at fault (verified by driving `_apply_input` against the real gated page:
  click, type, Backspace and Enter all landed, and Enter submitted the form). Three frontend
  defects, each independently sufficient to scramble real typing:
  (1) **Unordered delivery** — `sendInput` fired a fire-and-forget POST per event, and
  concurrent `fetch`es have no ordering guarantee, so a `keyup` could overtake its `keydown`
  and characters could transpose or vanish. Now a strict FIFO with one request in flight;
  `mousemove` is *coalesced in place* rather than queued (it's the only high-frequency event
  and a stale cursor position is worthless — queuing them starved the keystrokes behind them).
  (2) **Every key sent as `keydown`/`keyup`** — modifier presses were replayed separately (risking
  a stuck modifier), and a ctrl/cmd chord arrived as two unrelated key events. Keys now split
  three ways: modifier-only presses are dropped (`e.key` already carries the shifted character),
  a chord goes as one **`press`**, a printable character as **text insertion** (exact for
  symbols/unicode, no keymap guesswork), and only named keys (Enter/Backspace/Delete/Tab/arrows)
  as `keydown`+`keyup`.
  (3) **Paste had no handler at all** — replaying Ctrl+V remotely would paste the *server's*
  clipboard, which is never what's wanted. An `onPaste` handler reads the **local** clipboard and
  ships it as one text insertion.
  **Ctrl+V then still did nothing (fourth live report) — `preventDefault()` ordering.** The
  keydown handler called `e.preventDefault()` *before* its Ctrl+V early-return, and preventing
  the default on that keydown **cancels the browser's paste action outright**, so the `paste`
  event never fired and the handler above could never run. Measured both ways in a browser
  (`preventDefault=False → paste fires`, `True → nothing`). The Ctrl/Cmd+V bail now happens
  **before the event is touched at all**, in both keydown and keyup. Verified end-to-end that
  paste, plain typing, named keys and chords all coexist: one Ctrl+V delivered the full
  clipboard string (spaces and symbols intact) as a single insertion, with `a`/`b`, `Enter` and
  `Control+a` still dispatching correctly. Worth remembering generally: a blanket
  `preventDefault()` on keydown silently disables the whole clipboard path, and the failure is
  invisible — no error, the event simply never arrives.
  Backend gained the matching `press` type, with one measured special case: **headless Chromium
  does not apply the select-all editing command from a synthesized `Control+A`** (the selection
  stays collapsed, so Ctrl+A silently did nothing and a retype appended instead of replacing) —
  so select-all goes through the DOM (`activeElement.select()`) instead. Verified end-to-end
  against the real gated login page: per-character typing, Ctrl+A-then-retype replacing, bulk
  (paste-style) insertion into the password field, Backspace and Tab all correct.
- `quickjoiner/connectors/specs.py` — `FORM_SPECS` per-type field catalog (label/required/secret/
  env/list) + `connector_catalog()` (adds supported `modes`) driving the web-UI connector forms
  and capability stamps. **Keep field keys in sync with what each connector reads from `options`.**
  A type may also carry **`next_step`** (2026-07-24): what the user must do AFTER saving, for a
  connector that isn't usable the moment it's created. OneDrive needs an interactive Microsoft 365
  sign-in that *cannot* happen earlier (the token is keyed by source_id, so the connector must
  exist first) — the create form is field-driven, so without this the user is left hunting for a
  Sign in with Microsoft button that only appears on the saved connector's plate (reported live).
  `connector_catalog()` spreads the whole spec dict, so a key like this reaches the UI with no API
  change; the form renders it as an accent note above the fields and it replaces the generic
  "Sync it to start learning" post-create flash (which is actively wrong for OneDrive — syncing is
  not how documents get in).
  Each type also carries `suggests` (seed questions the type contributes to autocomplete —
  `suggest.py` reads them for configured source types); add them when you add a connector.
  **`extraction_prompt` — what these documents ARE, in the connector owner's words**
  (2026-07-30, user request; generic across all 13 types via the same guarded append as
  `aka`, `multiline=True` so the form renders a textarea — new `_f(multiline=)` flag +
  `TextArea` primitive + `ConnectorField.multiline`). A page carries its facts but not its
  *shape*: an internal service catalogue's entry reads as a bare table of names unless you
  already know the "Team" column means that team **owns** this repository, so the general
  extractor mines a fraction of what the page actually asserts. The person who connected the
  source knows the shape; this is where they say it. Threaded
  `options["extraction_prompt"]` → `pipeline._extraction_guidance(source_id)` (resolved once
  per drain wave from `list_source_configs`, best-effort — a catalog that can't answer just
  extracts unguided) → `triple_extractor(text, title, guidance)` →
  `triples.guided_system_prompt`, which appends it to `DOC_TRIPLE_SYSTEM` **fenced, capped at
  `_GUIDANCE_MAX_CHARS` (2000), and explicitly subordinated** to the rules above it: it is
  untrusted config text, so it may shape what the extractor *recognises* but cannot authorise
  a relationship the document doesn't state, and everything it yields still passes the same
  vocabulary + `RELATION_SIGNATURES` validation — a mistaken hint costs dropped lines, never
  an unvalidated edge. Unset ⇒ the prompt is byte-identical to before. Deliberately **not**
  `lock_after_sync` (unlike `aka`): it changes nothing already stored, only what the next
  pass looks for, so it is meant to be refined once you see what came out — re-run via a
  clean re-sync or `qj drain-graph <name>`. Needs `graph.extract_triples` on. The existing
  vocabulary already covers the catalogue case (`team | owns | repo`,
  `person | works_on | team`, `repo | part_of | project`), so this needed no vocab change.
  Tests: `tests/test_triples.py` (empty ⇒ identical prompt, hint present + subordinating
  sentence, length cap, off-signature/off-vocab lines still dropped under a "ignore the
  rules" hint, pipeline resolves it from the source config, missing source ⇒ "").
- **The agent is told what day it is** (`app._current_date_line`, prepended to
  `SYSTEM_PROMPT` in `build_agent`, 2026-08-10). It never was, and nothing in a grounded
  answer can supply it: a relative date ("on Friday", "yesterday's deploy", "last week")
  has no referent, so the model falls back on its training cutoff or simply invents one.
  Observed live in one 22-round turn resolving a single "Friday", it guessed
  **2024-05-21, then 2025-05-19, then 2025-05-20, then "Oct 21 2024"** — four mutually
  inconsistent "today"s — and never computed a date at all. That is a *silent* failure of
  exactly the kind this repo's grounding contract exists to prevent: a confidently wrong
  time window looks identical to a correct one that found nothing.
  **Date-only, deliberately no time of day**: Anthropic prompt caching hashes the whole
  tools→system→messages prefix as one unit, so anything that changes here misses cache on
  the NEXT request; a per-minute clock would invalidate every follow-up turn of every
  conversation, while a per-day grain costs at most one miss per 24h — the same order the
  codebase already accepts for session-compression invalidation. Rendered as
  `Current date: <Weekday>, <YYYY-MM-DD> (<zone>)` so a weekday question needs no
  arithmetic from an ISO date. Tests: `tests/test_agent_context.py`.
- **`agent/dates.py` + the `resolve_dates` tool — the arithmetic, not just the anchor
  (2026-08-10).** Knowing today fixes *where* to count from; it does not stop a model
  getting the count wrong, and that error is the dangerous one: a window off by a day or a
  week still returns real, well-formed, citable rows, so nothing downstream can detect it.
  `resolve(phrase, now, tz)` is pure and returns a half-open UTC `[start, end)` plus a
  **label** (what it resolved to, in words) and an **assumption** — non-empty exactly when
  the phrase was genuinely ambiguous and a convention had to be applied, which the prompt
  requires the agent to repeat. Conventions, all deliberate and all stated: days are
  **calendar days in `chat.timezone`** converted to UTC (a US-Central Friday is 05:00Z
  Fri–05:00Z Sat; resolving in UTC silently trims a working evening off each end); weeks
  are Monday-start; **"last week"/"last month" are the previous CALENDAR period while
  "last 7 days"/"last 30 days" are ROLLING** — different windows, both readings common, so
  the calendar ones name the alternative; **"last Friday" is strictly before today**, so
  on a Friday it is seven days ago, not today, while a bare "Friday" includes today; and
  **"this weekend" resolves BACKWARD when the current week's has not finished**, because a
  question about what happened cannot mean a future window (on a Monday "this weekend" and
  "last weekend" therefore agree — which is how people speak, not a bug). An unparseable
  phrase returns **None** and the tool tells the agent to ask rather than guess: inventing
  a window is the very thing this removes. `_span` rebuilds the end from the DATE rather
  than adding 24h, so a DST day stays a whole local day (25 hours on 2026-11-01). It also
  emits a **minutes-back-from-now** equivalent, because the grafana/datadog/dynatrace live
  tools take `minutes` rather than a range — closing that seam in one place instead of
  handing the arithmetic back to the model — and says plainly that minutes-back runs to
  now and so sweeps up everything since the window ended. Date grounding was extended to
  the two other prompts that needed it: `briefs.py` (the `week1`/`roadmap` specs ask what
  is in flight and upcoming, and the date was computed AFTER the model call and used only
  for the header, so the model never saw it) and `sessions.py`'s distillation prompt, the
  most durable case — a `FACTS:` line becomes a permanent citable memory document, so
  "deployed yesterday" is unrecoverable and relative dates must be resolved before
  recording. `chat.timezone` (IANA, default UTC) is the zone; `tzdata` is declared for it
  since Windows ships no tz database, and an unavailable zone degrades to UTC **and says
  so** rather than silently shifting every window. Tests: `tests/test_dates.py` (39).
- `quickjoiner/agent/` — grounded system prompt (`prompts.py`), built-in tools
  (search_memory/remember/list_sources + graph_neighbors/**graph_relations**/graph_path in
  `tools.py`; **`graph_relations(rel, src_type, dst_type)` is the ENUMERATION read** —
  every edge of one relation across the whole graph, grouped by its right-hand entity, for
  "list all teams with their members" / "which team owns which repo". Neither existing graph
  tool served it (`graph_neighbors` is one entity, `graph_path` is two) and neither does
  memory: search returns the top-k most *similar* chunks, so on the live corpus a single
  `/Teams` overview page took slot 1 and **seven chunks of one 323-person roster** took the
  rest, leaving zero of the 17 per-team detail pages in an 8-chunk window — names and counts
  came back, members never did, and asking per-team worked only because the team name pulled
  its own page to top-2. Bounded at 400 with truncation **stated**, not silently sampled.
  Same pass fixed a real `graph_neighbors` defect: the hub branch grouped by relation
  ALONE, so a team with 39 outgoing `works_on` (its projects) and 15 incoming (its people)
  shared one bucket ordered by `dst` — every one of the 8 sampled rows was a project and
  not one member appeared. Groups are now keyed by **(relation, direction)**, rendered
  `--rel-->` / `<--rel--`, each with its own budget, and point at `graph_relations` for the
  complete list),
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
  **Per-source breakdown (`divergence.py`, pure, 2026-08-05, user-reported):** "list all
  teams and their members" is answerable from a catalogue, from Confluence charters and from
  TFS, and the user got one merged answer with no sign the alternatives existed. The
  knowledge graph returns a **union** of typed edges, so no single system necessarily claims
  the merged line — and nothing could reveal that until the evidence document's `source_id`
  was carried on the edge row (`evidence_source_id`, one column added to the existing LEFT
  JOIN in `_EDGE_SELECT`; portable, no schema change, **verified on pgvector**, not just
  statically reviewed). `membership_by_source(rows)` re-derives what each system asserted for
  ONE `graph_relations` group; the tool appends a `per <system>:` breakdown and a note.
  **Two things measurement killed, and they are the point of this entry.**
  (1) **The obvious retrieval-side rule does not work and was removed, not tuned.** "Several
  sources have competitive hits ⇒ several systems each answer this" was built first, then
  measured on the real 11-connector corpus: it fired on **80% of ordinary questions**, and a
  threshold sweep (score band × minimum documents per source) found **no setting separating
  the classes** — recall 1.00 came with 75% false positives, and forcing noise to 6% cost all
  recall. "What does X depend on" legitimately draws on TFS + a repo + a wiki *jointly
  building one answer*, which from provenance alone is identical to three systems each
  answering alone. The signal is absent, so `search_memory` announces nothing; the
  `[source: …]` label already on every chunk carries it for free.
  (2) **A naming variant is not a disagreement, and differing coverage is not a conflict.**
  The first graph-side cut compared raw names and flagged groups CONTESTED: measured, three
  of the top hits were one team spelled two ways (`AppRiver\SecureCloud 2.0\Caffeine` vs
  `Caffeine`), so `fold_member` compares last path segments — which alone took
  `owns team->project` from 2 "conflicts" to **0**. And three systems listing different
  services in an environment are covering different parts, not contradicting each other, so
  the wording states the lists differ and explicitly says **you cannot tell disagreement from
  partial coverage** — it never calls a system wrong. Final rates on the real graph: 5%
  (`works_on`), 1% (`owns team->repo`), 0% (`owns team->project`), 9% (`deploys`).
  `ledger_entries_for_hits` still runs on every grounded search, **invisibly** — it renders
  nothing and changes no answer, and exists so a candidate the model does offer carries a
  real number instead of **"unscored"**, which before this was true of everything except a
  graph path (only `graph_path` populated the ledger). Best-effort throughout: scoring can
  never break a search. Tests: `tests/test_divergence.py` (14, incl. every silence case —
  one system, matching membership, spelling variants, a `same_as` bridge with no evidence
  source — the no-announcement guarantee on `search_memory`, and an end-to-end split
  verified to fail with the provenance column blanked).
  **operational tools = the agent-tool bridge** (`ops.py`: scrape_website /
  list_connector_types / add_connector / sync_source — same service functions as the UI
  slash commands; prompt requires explicit user confirmation before add_connector, secrets
  via env: indirection only, scrape only user-given URLs, failed connection tests are not
  saved; wired in `AppContext.build_agent`),
  **self-control tools = `/qj` natural-language control of QuickJoiner's OWN API** (`control.py`,
  plan 08, 2026-07-21): `qj_api(method, path, body, confirm)` dispatches against the real API
  **in-process** (Starlette `TestClient` over the same FastAPI app + `AppContext` — no network, no
  duplicated handlers) and `qj_api_reference(area)` lists the endpoints the caller may use (role-
  filtered) plus the connector field schema (so "create a git connector" is one discovery call).
  Three gates before anything runs: RBAC capability (`rbac.py`) → connector scope
  (`visible`/`can_manage`) → danger-confirm (`memory:reset`/`connectors:delete` need typed
  `confirm=true` on top of the prompt's conversational confirmation for any mutation). SSE
  endpoints are refused. The acting user is injected into the in-process request via a
  per-process secret header (`_internal_user` middleware in `api/app.py` sets a contextvar
  `_user` honours; a network client can't forge it). Wired in `build_agent(user, role)` (role
  derived via `role_of`). The **permanent control connector** (`connectors/self_connector.py`,
  type `quickjoiner`) is seeded as a commons singleton that yields no documents (never ingested,
  never in the graph) and is un-deletable/un-renamable/un-syncable (409 guards) — it's the plate
  that surfaces this capability. Tests: `tests/test_control_tools.py` (gating, danger-confirm,
  scope, role-filtered reference, seeded+undeletable connector, route-coverage lockstep). Old
  `/connect` + `/learn` slash commands are retired in favour of `/qj`. Also: tool-call loop
  (`agent.py`, max 10 rounds —
  **each live tool result is capped to `chat.live_tool_result_max_chars` (default 24000) before
  re-entering the model context**, so an unbounded connector tool like the full Octopus dashboard
  can't overflow the window and make the provider reject the follow-up turn — **except
  `graph_relations`/`graph_neighbors`/`graph_path` (`AppContext._UNCAPPED_TOOLS`, 2026-08-07,
  user-reported "you missed some teams")**: those three already bound themselves to a small,
  fixed shape (400 relationships / a hub sample / ≤3 path chains) with no model-controllable size
  parameter, and each states its own truncation explicitly in the text it returns. The blanket
  char cap doesn't know that — it slices by raw length regardless — so a `graph_relations` call
  that returned a complete, correctly-labelled 334-row list (under its own 400 cap, so its own
  "truncated" flag never fired) was still chopped mid-list by the outer cap, replacing its honest
  "not truncated" state with a generic `[tool output truncated]` marker and silently dropping
  whichever teams fell after the char cutoff — the model then reported a subset as if it were
  complete. `OnboardingAgent._cap(output, tool_name)` now skips the char cap for tool names in
  `uncapped_tools`; `search_memory` and the live connector/ops/control tools are deliberately NOT
  exempted (`search_memory`'s `top_k` is model-controllable, so it has no comparable hard bound —
  it stays the case this cap exists for). Tests: `tests/test_agent_loop.py`
  (`test_uncapped_tool_bypasses_the_char_cap`). Suite: **878 passed** (+2), 15 skipped, pre-existing
  Postgres env-gate unchanged. Not yet observed live against the reported 334-relationship
  question — the fix is verified at the unit level (cap is skipped for exempted tool names, and
  `graph_relations`'s own 400-row/truncation-notice contract is unchanged and separately tested);
  on hitting the round
  limit the agent makes **one final tool-free turn** so a model that loops on searches still
  answers or properly refuses from what it gathered, instead of a canned "hit the limit" message.
  **Trace events — the run as an ordered timeline (2026-08-08, user request):**
  `tool_call` used to carry the bare tool NAME, and nothing carried the outcome, so the UI
  could show *that* `search_memory` ran but never what it searched for or what came back —
  and reasoning arrived on a separate channel, so the ordering between the two was lost.
  `tool_call` now carries `{id, name, args}` and a new **`tool_result`** carries
  `{id, name, ok, summary, chars}`, paired by `id` and emitted around the same `tool.run`
  call, so `thinking` / `tool_call` / `tool_result` interleave in the real order the run
  happened — which is the whole contract the step-by-step trace UI is built on. Both are
  **display-only**: the model still receives the full (or `_cap`-limited) output, so nothing
  here can change an answer. Bounded on purpose (`_ARG_PREVIEW_CHARS` 400 per argument,
  `_RESULT_PREVIEW_CHARS` 800) — a single result can be 24k chars and there is no reason to
  push it down the wire twice — and `chars` states the true size so a clipped preview is never
  mistaken for the whole output. `chars` measures the output **as the model received it**
  (after `_cap`, whose own truncation marker rides inside it), not a pre-cap size the model
  never saw. Tests: `tests/test_agent_loop.py` (pairing + ordering, a raising tool reported
  `ok: false`, and the bounds with the true size).
  **Citable sources reach the CLIENT, so a citation can be a link (`agent/refs.py`,
  2026-08-08, user request — "in ROVO sources appear as clickable links to the actual
  pages"):** `search_memory` has always handed the MODEL a uri per hit
  (`[source: <label> | uri: <uri> | kind: <kind> | score: <n>]`), and the client saw none
  of it — the answer text carries only whatever label the model chose to write. So a
  citation of a readable title had nothing to link to, while a citation of the raw URL was
  clickable but unreadable. `parse_source_refs` (pure) reads those headers back out of each
  tool result and the agent emits them as a **`sources`** event (`{label, uri, kind?,
  score?, snippet}`), additive per turn and deduped by `(label, uri)` so repeated searches
  don't re-send the same documents. Both header shapes parse — the `search_memory` block
  form and the graph-expansion `- [source: … | uri: …]` bullet, which has no kind/score.
  The snippet is the source's own opening text: bounded, word-boundary cut, stopped at the
  next hit's separator so one source can't quote another's, and with the
  **contextual-chunking breadcrumb stripped** (`[<source_id> · <title> · <uri>]` is
  provenance for the vector, and as an excerpt it merely repeats the title and link shown
  beside it). A header with an empty uri is still returned — worth naming in the panel,
  simply not linkable.
  **The graph tools cite with a uri too (`_evidence_ref`, same day, user-reported "I don't
  see citations as links" on a question the first cut had left half-broken):** an edge row
  already carried `evidence_uri`, but the renderer preferred the title in an `or` chain and
  **discarded the url** — so the SAME document behaved differently depending on which tool
  cited it, and a question routed to `graph_relations` (which is exactly what "list all
  teams with their members" is) had most of its citations resolve to nothing. Measured on
  the live corpus: **5 of 11 citations linked before, 9 of 12 after**, with refs announced
  per turn rising 13 → 69. All three evidence sites now render
  `[evidence: <title> | uri: <uri>]`, and `graph_relations` emits **one bracket per
  document** instead of a semicolon list inside one — a list cannot be parsed into
  (label, uri) pairs, and it also invited the model to cite a run-on string naming two
  pages. Evidence with no uri (a `same_as` bridge, a code-graph file path) still renders
  bare and is deliberately NOT matched: there is nothing to link to.
  **`weblinks.py` — identity is not an address (2026-08-08, user request: work items,
  pipelines, MRs and *code files* should all be citable links).** A document's `uri` is
  what identifies it, and three shapes are not browsable: a cloned repo file is
  `<clone-url>::<path>` — which **begins with `https://`**, so any scheme-only check
  offers it and it 404s with total confidence (a real regression the first cut shipped);
  `file://` is blocked by browsers from an http page, so it fails silently; and
  `conversation://` is internal. `citable_link(uri)` (pure) returns the URL to open or
  **None**, and the ref carries it as a separate **`link`** field — `uri` stays identity,
  and the client links on `link`, never on `uri` looking like a URL. Repo files are
  rebuilt from each forge's own web layout (`/-/blob/<ref>/` GitLab, `/blob/<ref>/`
  GitHub, `/src/<ref>/` Bitbucket, `?path=` Azure DevOps), from either an https or an scp
  (`git@host:group/repo.git`) remote, with embedded credentials stripped so a token can
  never reach a rendered link. **`HEAD` is the ref** — the connector's `branch` option is
  usually blank ("whatever the remote's default is") and all three forges resolve HEAD to
  the default branch. An **unrecognised host yields None rather than a guessed path**:
  every forge spells its blob URL differently, so inventing one 404s while looking
  authoritative. Verified on the real corpus — **1200 git documents across 3 repos, all
  deriving clean blob URLs, zero malformed** — and the derived (project, path, ref)
  triples were confirmed to name **files that actually exist** via the GitLab API, since
  a plain HEAD request only 302s to sign-in and proves nothing.
  **Live tools became citable the same way (`live_tools.cite`)**: they answer the
  enumeration questions memory can't, but a bare "!123 Fix login" resolves to nothing, so
  their citations could never link the way an ingested document's could. `cite(label,
  url)` emits the same `[source: <label> | uri: <url>]` marker `search_memory` does — one
  parser reads both — wired into GitLab MR/issue/commit/pipeline/job lists (which already
  had `web_url`) and Azure DevOps work items + builds (whose URLs are **constructed** from
  `org_url`, matching the ones the ingested work-item documents already carry, so a live
  hit and a learned hit cite identically). It returns `""` when there is no url: a marker
  with nothing behind it is noise in the model's context and promises a link that cannot
  exist. Delimiters in a label are neutralised, since ticket titles routinely contain
  `|` and `[]`. Applied to **GitLab, Azure DevOps and Octopus** live tools; the Octopus
  dashboard tool formats its own lines rather than reusing `dashboard_document().text`,
  because that document's text is chunked and embedded and a marker inside it would later
  be read back out of search results as though the stored page were itself citing sources
  — markers belong in live tool output only. Tests: `tests/test_weblinks.py` (14).
  **Measured before building any of it, which changed the scope:** Octopus (995/995) and
  web-scraped pages (565/565) already carried real URLs and were *already* linking, so the
  only genuine gap was the Octopus live tool. What the audit did surface was a
  **misattribution risk**: 23 of the live crawl's documents share the title
  "Home page - AppRiver.ContinuousDelivery" (they predate the `<h1>` title fix, which only
  takes effect on re-ingest), and label matching was first-wins — so citing that title
  linked to whichever of the 23 happened to arrive first. `CiteBook` now indexes a label
  to EVERY distinct page retrieved under it: one ⇒ link, several ⇒ **ambiguous**, no
  inline link, and the sources panel lists all the candidates with a note. The same map
  also indexes the **head of a `"<name>: <description>"` title**, since models cite the
  head (observed live: `[Octopus deployment dashboard]` against a title ending
  `: current state per project/environment`) — safe precisely BECAUSE collisions register
  as ambiguity, so "Octopus project", which prefixes hundreds of documents, resolves to
  nothing rather than to an arbitrary one. `-` is deliberately not a separator: it appears
  inside real titles far too often ("MailStore - Team Charter").
  ⚠ **`candidates._REF` had to move in lockstep** — its evidence branch captured to `]`,
  so the new `| uri:` tail would have been folded into the ref NAME and every candidate
  citing a title would have silently stopped resolving. Both branches now stop at the
  first `|`. That one-bracket-per-document change also broke the snippet and was caught
  only by looking at the rendered panel: the text following the first bracket is the
  SECOND bracket, so the excerpt showed raw `[evidence: … | uri: …]` markup — `_snippet`
  now drops leading sibling citations and stops at the next one. Tests:
  `tests/test_source_refs.py` (14, incl. the graph-evidence regression, the no-uri guard
  and the sibling-citation excerpt) + agent-level emission/dedupe/silence tests.
  **Empty-completion guard (2026-07-23):** a round that returns NO tool call AND blank text — a
  reasoning model (gpt-oss) that emitted only a thinking channel, or an empty completion — no longer
  returns that blank ("qj ended with no response"); it breaks to the same final tool-free turn, and
  if THAT is also blank a graceful fallback message is returned. The candidates-carousel case
  (non-blank text whose prose is stripped to empty) is untouched. Tests: `tests/test_agent_loop.py`),
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
- **Document labels + scoped questions (2026-07-25, user request).** Three connected pieces:
  **(1) See it** — `GET /api/sources/{source_id}/documents` lists what a connector actually
  ingested (uri/title/chunks/updated), each row carrying the labels that apply to it; the
  **`DocumentsModal`** renders it grouped by folder, opened by clicking a Connected-systems row
  in the Rail. **(2) Label it** — ONE catalog table `doc_labels(source_id, uri_prefix, kind,
  value)` covers all three granularities through `uri_prefix`: `''` = the whole connector, a
  folder path = that folder, a full uri = one document. A folder label is a **prefix RULE
  resolved at query time** (`label_applies`, pure), not a snapshot copied onto the rows that
  existed when you tagged — which is what makes documents ingested *later* inherit it with no
  re-tagging (the behaviour the user asked for, and the reason this isn't a join table).
  `kind` splits the two jobs: `tag` is a scoping label; `aka` is an alternate name, and a
  **connector-wide** aka is additionally registered as a source-entity alias so it feeds the
  existing `resolve_entity`/query-expansion path (folder/document akas are NOT — they name a
  document, not the source entity, and claiming otherwise would misdirect the graph).
  `_like_prefix` escapes `%`/`_` because a real Windows path or URL can contain them and an
  unescaped LIKE would silently widen the match. **(3) Ask within it** —
  `memory.store.SearchScope(source_ids, doc_ids)` threads into **both hybrid legs of both
  backends**: LanceDB `where(..., prefilter=True)` (so the predicate runs BEFORE the ANN
  search — genuinely less work, not over-fetch-and-discard), the FTS5 sidecar's `WHERE` over
  its UNINDEXED `source_id`/`doc_id` columns, and pgvector's `WHERE`/`= ANY` on both legs.
  `catalog.resolve_scope` turns a user's picks into those ids **once, server-side**, so no LLM
  round-trip is spent working out what "the Zix deck" means — and a whole-connector tag
  resolves to a *source_id* rather than enumerating documents, keeping the predicate O(1) in
  corpus size. (`SearchScope` gained a second, AND-ed `visible_source_ids` dimension in
  2026-08-04's knowledge-scopes work — see the auth bullet; picking and being permitted are
  different kinds of narrowing and compose rather than override.) `build_agent(scope=…)` additionally **withholds live connector tools for
  sources outside the scope** (`_scoped_sources`), which is where the saved round-trips
  actually come from: the model cannot call into a system the user excluded. A scoped refusal
  is deliberately worded differently from a global one ("not in the sources you scoped to",
  never "the organisation never learned this") — it only searched a slice, and saying
  otherwise would be a false claim. UI: **`ScopePicker`** chip in the composer (default "All
  memory", sticky across a conversation, one click to clear), offering connectors with
  documents plus every tag. `POST /api/chat` takes an optional `scope`; omitting it is
  byte-identical to the old request. Tests: `tests/test_scoping.py` (17). NB this is also the
  filtering machinery per-user knowledge scopes (PRIORITIES #2) needs.
- `quickjoiner/skills/` — **Agent Skills: packaged expertise, in the open format Claude Code and
  GitHub Copilot both read** (2026-08-07, user request). Connectors teach the system what the org
  knows; a skill teaches it **how the org works** — the query syntax for a log index, which ticket
  fields a team actually uses, a release runbook. A skill is a folder with `SKILL.md` (YAML
  frontmatter + markdown), optionally `references/` and `scripts/`. Format compatibility is a
  design constraint, not a bonus: a skill written for either tool works here unmodified and stays
  working there, so nothing here is QuickJoiner-specific and the two QuickJoiner additions
  (`requires_env`, `scope`) are **optional frontmatter keys**, which the other runtimes ignore.
  `loader.py` — discovery + parsing, deliberately side-effect-free (it runs on every request that
  builds an agent, so one malformed folder must never take the catalogue down: `load_skill`
  returns None rather than raising). `discover(roots)` reads `<workspace>/skills` (the portable
  root, where uploads land) then `~/.claude/skills` and `~/.copilot/skills`, first origin winning
  a name clash so a workspace skill deliberately shadows a personal one rather than the order
  depending on the filesystem. **`detect_env` is a SUGGESTION, never a declaration** — it reads
  a skill's scripts for environment reads so a person adopting an existing skill is shown "this
  looks like it needs TFS_PAT" instead of having to read them. Two things keep it usable rather
  than noise, both found against a real library: the read pattern is **scoped to the language**
  (`${NAME}` is a shell env read but JavaScript template interpolation, and matching it everywhere
  turned one real skill into 90 lines of local variable names), and `_SYSTEM_ENV` drops what the
  OS provides (LOCALAPPDATA and ProgramFiles were the two most common reads across the whole set).
  It still cannot tell a credential from a tuning flag — measured on the real `tfs-control` skill,
  it proposes 9 values of which 4 have defaults — which is exactly why the admin PATCH exists and
  why the UI offers the extras as "add them if they are credentials".
  `registry.py` — **the folder seeds QuickJoiner's configuration; the `skills` table is
  authoritative thereafter.** An uploaded skill's frontmatter is written by whoever wrote the
  skill, so an admin must be able to narrow or widen it without editing files on the server; the
  scope survives rediscovery and reinstallation. On first sight, scripts reading no credentials ⇒
  **open** (anyone), any credential ⇒ **user-scoped** (only someone who supplied their own).
  `secrets.py` — per-user values layered **user > workspace > process env**, so a shared
  `OCTOPUS_URL` sits at workspace level while each person supplies their own `OCTOPUS_API_KEY`.
  **Encrypted at rest** (Fernet, key in `<workspace>/secrets.key`), unlike connector options which
  are stored plaintext and merely masked on read — the difference is deliberate, since these are
  individual people's own credentials and a workspace database gets copied for backups and bug
  reports; a copied `catalog.db` alone yields nothing. Honest about the limit: anyone with BOTH
  the database and the key file can decrypt, and the server must be able to, so this is protection
  against a leaked copy, not against someone with the machine. **The rule that matters most:
  `resolve(..., include_process_env=False)` for a user-scoped skill** — a credential the SERVER
  holds is not this person's, and falling back to it would run the skill as somebody else while
  looking like success, so the value is reported missing and the agent names it.
  `runner.py` — the one part that executes code, so its rules are narrow and stated: the script is
  resolved and confined to the skill folder (the name comes from a model), **no shell** (argv
  list, so a filename can never become a command), wall-clock timeout + truncated output, and
  **secret values are never echoed** — a failure names only the KEYS that were missing. What it is
  NOT is a sandbox: a script runs with the server's privileges, which is why authoring is an admin
  act while *using* a skill is open to everybody.
  **`encoding_hint` — naming a failure the interpreter blames on the wrong line
  (2026-08-10, hit debugging a real skill).** `pwsh` is preferred, but where it is absent
  `.ps1` falls back to **Windows PowerShell 5.1**, which has no `-Encoding` for `-File`
  and reads a BOM-less script in the system ANSI codepage. A UTF-8 em dash is then three
  cp1252 characters ending in **U+201D — a smart double quote, which PowerShell accepts as
  a string delimiter** — so one em dash inside a double-quoted string silently terminates
  it and the parser reports `Missing closing '}'` against an unrelated line dozens of
  lines away. Reproduced minimally (`Write-Output "a — b"` inside an `if` block ⇒ exactly
  that error). Nothing in the message mentions encoding, so it reads as a brace bug that
  is not there. The hint is **appended to a failure, never a refusal** — a genuinely
  ANSI-encoded script with the same bytes runs fine, and a false refusal would cost more
  than a redundant note — and is added AFTER output truncation so a verbose failure cannot
  push the diagnosis off the end. Fires only when the run failed, the fallback was used,
  the file has no BOM, and the bytes decode as UTF-8 with non-ASCII present. Tests:
  `tests/test_skills.py` (the note, three no-note cases, and survival of truncation —
  verified to fail with the append removed).
  ⚠ **Two authoring rules a skill's scripts must follow, both found the same day and both
  independently fatal**: a script that calls `Read-Host` on a path the agent reaches is
  unusable (5.1 is launched `-NonInteractive`, so it throws) — prompt only when there is a
  real console AND stdin is not redirected, and default everything else; and **`SKILL.md`
  must document its scripts' parameters**, because `open_skill` shows the model only the
  filenames. Given a script and no signature, a model guesses, and the guess lands in
  whichever parameter is positionally first.
  **`split_args` — neither shlex mode is correct, and both are wrong silently
  (2026-08-10).** Arguments arrive as ONE string (tool schemas vary in list support across
  providers) and were split with `posix=False`, which preserves Windows backslashes but
  **leaves the quote characters inside the token** — measured end to end, `-SearchTerm
  "subscription installed"` reached the script as `['-SearchTerm', '"subscription
  installed"']`, quotes included. The other mode is no better: `posix=True` treats a
  backslash as an escape, so `-Path C:\Users\x` arrives as `C:Usersx`. So: split
  non-posix, then strip ONE matching pair of surrounding quotes per token. `""` stays a
  deliberate empty argument, an inner quote (`'{"a":1}'`) survives, and unbalanced quotes
  degrade to a whitespace split rather than raising — the string is model-authored.
  `_rejoin_equals_quoted` additionally re-quotes the `=`-joined GNU/.NET spelling
  (`--key="a b"`) around the WHOLE argument before splitting, since a quote that does not
  START a token is interior to shlex and the value would split on the space inside it;
  quoting the whole thing preserves `--key=a b` as one argument rather than rewriting it
  to two, which a parser accepting only the joined form would reject.
  **`stdin` — the escape hatch for a script that prompts and cannot be changed
  (2026-08-10).** `run_script(stdin=)` / the tool's `stdin` parameter feed answers one per
  line. Deliberately framed as a LAST RESORT in the tool description — answering blind
  guesses the prompt order — but it makes an unmodifiable interactive script usable
  instead of dead, verified against the original broken `Query-AppLogs.ps1`. Two details
  make it actually work: PowerShell is normally launched **`-NonInteractive`**, under
  which `Read-Host` throws no matter what is on stdin, so that flag is **dropped for this
  run only**; and with NO stdin supplied the child gets **`DEVNULL` rather than the
  server's inherited stdin**, so a prompting script hits EOF and fails in a second instead
  of blocking for the full 120s timeout and reporting only "timed out". Clipping at
  `MAX_STDIN_CHARS` is stated in the output, because a silently truncated answer is a
  *plausible wrong* answer to a prompt rather than an error.
  ⚠ **A skill's NAME and its FOLDER need not agree, and deriving the delete target from
  the name made such a skill undeletable everywhere (2026-08-10).** `load_skill` reads the
  name from frontmatter and falls back to the folder only when that is missing — and the
  CLI explicitly invites dropping a folder into `<workspace>/skills`. So a folder `logs/`
  declaring `name: query-app-logs` listed fine under the declared name while its files
  lived elsewhere, and `uninstall(workspace, name)` computing `skills/<name>` found
  nothing: **CLI, API and UI all failed at once**, while the API row's `removable: true`
  went on promising otherwise, and the 400 raised before `delete_skill_config` so the
  catalog row survived too. `uninstall` now takes the **discovered `skill.path`** (still
  confined to the workspace skills root, so it cannot become a delete-anything primitive).
  Zip installs always agree — the archive is unpacked into a folder named after the
  declared name — which is exactly why the suite never caught it.
  ⚠ **What the author STATED outranks what detection guessed (2026-08-10).**
  `requires_env: []` is an author saying "this needs nothing", and `_env_list` returned
  `()` for it — indistinguishable from the key being absent — so `skill.requires_env or
  detect_env(skill)` let the heuristic override an explicit declaration. It now returns
  **`None` for absent and `()` for explicitly empty**, and the `scope:` frontmatter key —
  documented in `loader.py`'s own header from the first commit and **never actually
  parsed** — is now read, with `scope: open` overriding detection outright. This matters
  because the failure is total and silent in the wrong direction: detection cannot tell a
  credential from a base URL or a tuning flag (measured on the real `tfs-control` skill: 9
  proposed, 4 with defaults), and one such read made the skill `SCOPE_USER`, which
  resolves with `include_process_env=False` — so it was `UNAVAILABLE` to **every** user,
  including where the server already held the value, fixable only through an admin PATCH.
  `install.py` — a skill arrives as a zip (the shape GitHub, Claude Code and Copilot all hand
  you). Every member is resolved and checked to stay inside the target (a naive `extractall`
  writes `../../etc/…`), symlink entries are dropped since they are a path escape that survives
  the name check, and member count / per-member / total-uncompressed size are bounded — the same
  decompression-bomb shape `ingest/extract`'s archive path already guards. **Unsafe members are
  filtered BEFORE the common root is computed, not just before writing** — found by probing a
  mixed archive rather than by the suite: one stray `/etc/passwd` entry made the top-level folders
  disagree, defeated root detection, and rejected an otherwise perfectly valid skill with a
  misleading "no SKILL.md" for an archive that plainly had one. Containment was never at risk;
  usability was. Extraction **stages
  then swaps**, so a failure part-way leaves the installed skill intact rather than a half-written
  folder discovery would happily load; a reinstall **replaces the folder outright** so a script the
  new version no longer mentions cannot survive to be run, while the stored configuration for that
  name is untouched. `uninstall` only ever writes under the workspace root: a skill found in a
  personal `~/.claude/skills` belongs to that person's own tooling (409 from the API, "disable it
  instead").
  `tools.py` — **progressive disclosure is what makes a large library affordable**: only NAME and
  DESCRIPTION ride the system prompt (`catalogue_prompt`, bounded at `MAX_DESCRIPTION_CHARS` 280
  — measured on a real library, one skill's description alone ran to ~1,000 characters and seven
  of them would have cost more prompt than most answers), and the body is fetched only when the
  model decides a skill applies. Four tools: `open_skill` (the body), `read_skill_file` (one
  bundled reference), `run_skill_script`, and `my_skill_secrets` — the last exists so the agent
  can answer "why can't you do that?" by naming the variables the person must set instead of
  failing vaguely. **An unavailable skill is still LISTED**, marked so, for the same reason:
  hiding it would leave the agent unable to explain a capability the person can see in the UI.
  Wired in `AppContext.build_agent` via `skill_tools(user)`, best-effort — a broken skill folder
  or secret store costs the skills feature, never the whole agent. API: `GET /api/skills` (rows
  carry per-CALLER `ready`/`missing`), `POST /api/skills` (multipart .zip), `PATCH`/`DELETE
  /api/skills/{name}`, and `GET`/`POST`/`DELETE /api/skills/secrets…`. RBAC adds three caps
  rather than the usual read/write pair, because "configure the library" and "set my own password
  for a skill" are genuinely different acts: `skills:read` + `skills:secrets` are **viewer**-tier
  (using a skill and supplying your own credentials are open to everybody — the point of the
  feature), `skills:write` is **admin**-only. UI: `frontend/src/components/SkillsPanel.tsx`, a
  Settings → Skills section; the credential form is write-only throughout (a key already set shows
  as "yours"/"workspace" with a Replace box, never a populated field that would imply we could
  read it back). Tests: `tests/test_skills.py` (30 — written as security tests: a scoped skill
  never borrowing the server's credentials, one user's secrets not enabling another, a
  model-supplied path escaping neither `read_skill_file` nor `run_skill_script` nor the installer,
  a value never readable back out, admin configuration surviving rediscovery AND reinstall) + 8 in
  `tests/test_api.py` (per-caller readiness, the admin/viewer split, workspace-vs-personal scope).
- `quickjoiner/auth.py` — opt-in local auth. `Auth` over the catalog: PBKDF2 password hashing,
  bearer tokens (sha256-hashed at rest in `auth_tokens`), `users` table (with a **`role`
  column**). **Open mode until the first user exists** (no login, everything shared = pre-auth
  behavior). Sharing model on `SourceConfig` (`owner`, `shared`): ownerless = commons; owned =
  owner-only unless `shared`. `visible()` / `can_manage()` are the gate. Ingested *knowledge*
  stays one communal memory; sharing governs who sees/manages a **connector's config +
  credentials** and gets its live tools.
  **RBAC (plan 08, 2026-07-21):** `Auth` gained `create_user(role)` (**first user is always
  admin**; later users default `viewer`), `set_role`, `get_role`, and `role_of(user)` (open mode
  ⇒ admin for all; enabled auth ⇒ the stored role, anonymous/unknown ⇒ least privilege). The
  role → capability model lives in **`quickjoiner/rbac.py`** (pure, dependency-free): roles
  **admin / editor / viewer**, a capability taxonomy derived from the OpenAPI tag groups
  (`connectors:write`, `sync:run`, `memory:reset`, `settings:write`, `users:admin`, …), and a
  `(method, path) → capability + connector-scope` map (`required_capability`, `can`) covering
  every `/api` route — a **route-coverage lockstep test** asserts none is unmapped. `connectors:
  delete`/`memory:reset`/**`skills:delete`** are the danger tier (`rbac.DANGER_CAPS`) — each
  needs a typed `confirm=true` from the chat path on top of the conversational confirmation.
  `skills:delete` is split out from `skills:write` for exactly the reason `connectors:delete`
  is split from `connectors:write`: same admin role, but deleting files is not the same act as
  installing them, and it is reachable from chat where "tidy up the old skills" must not become
  an unconfirmed `rm`. Enforced in **two** places: the control tool
  (`agent/control.py`, the chat path) and the HTTP layer (`_require(cap, user)` on every mutating
  route, placed after existence/visibility checks so a private resource still 404s rather than
  leaking via 403 — open mode is a no-op since everyone is admin). Connector-specific ops also
  require the source be visible/manageable (reuses `visible`/`can_manage`). Tests:
  `tests/test_rbac.py` (model + lockstep), role gating in `tests/test_auth.py`.
  **Knowledge scopes — per-user visibility over ONE communal store (2026-08-04, PRIORITIES #1,
  Cloud Y1.8 / PRD W9.3; the enforcement core — see the roadmap for the two remainders).**
  Ingested knowledge used to be one flat commons: right for single-user, wrong the moment the
  OneDrive connector shipped, because a delegated Microsoft 365 token reads exactly what one
  *person* can read — **including files shared privately with them** — and everything it learned
  became retrievable and citable by everyone. The connector warned about that in three places;
  a warning is not a control, and this is the control.
  **The scope key is the existing `documents.source_id -> sources.owner/shared` chain, so there
  is no doc-schema change and no migration.** `catalog.visible_source_ids(user)` is the one
  definition of what a person may read (ownerless commons + shared + own), deliberately over
  **all** source rows rather than just configured connectors — an ingestion bucket is a source
  too, and is exactly what has to be filtered once a taught note can be private.
  `AppContext.visible_source_ids(user)` returns **None** in open mode, which means "no
  visibility predicate at all", so a single-user workspace's query text is byte-identical to
  before and pays nothing.
  **`SearchScope` grew a second, independent dimension** (`visible_source_ids`) beside the
  picked `source_ids`/`doc_ids`. The two are **AND-ed, never OR-ed** — OR-ing would let naming
  a source you cannot see escalate into reading it — and visibility is never widened by
  picking nothing. `None` = unrestricted; `[]` = may read nothing and must match **no** row
  (the naive spelling of that is `IN ()`, which is invalid SQL and would have failed the
  filter *open*, so every renderer turns an empty list into an explicit false predicate).
  `predicate_groups()` is the single structural definition; LanceDB inline SQL, the FTS5
  sidecar's `?` params and pgvector's `= ANY(%s)` differ only in how they spell a list.
  `has_picks()` is separate from `is_empty()` on purpose: a visibility-only scope filters
  memory but must NOT read as a picker selection, or it would strip the live connector tools
  the user never excluded — and a refusal is worded differently for the two ("not in the
  sources you scoped to" is a lie when the user scoped nothing).
  **Retrieval is not the only channel, and the others are where the real leaks were.** All of
  these now carry `visible_source_ids`: `graph_neighbors` / `graph_relations` / `graph_path` /
  `graph_path_candidates` / `graph_expand` / `edge_corroboration` / `entity_evidence` /
  `graph_snapshot` / `graph_totals` (`_evidence_visible`, keyed on the evidence document's
  source), plus `search_entities` / `bridge_entities` (`_visible_entity_filter`, a correlated
  EXISTS — **an entity NAME is disclosure on its own**, so filtering edges while autocomplete
  still completed `person:Mole` would have leaked the interesting half), the gap near-miss
  probe (the backlog is read by everyone), and `QuestionSuggester.suggest`. Corroboration
  counts only visible evidence — confidence is shown to a person, and a number they cannot
  audit is a claim we can't back. Totals are counted over the asker's own graph, since a
  global denominator both overstates their view and discloses how much is withheld.
  Two deliberate rules inside `_evidence_visible`: an edge with **no** evidence document
  survives (a `same_as` bridge is derived from entity names, cites nothing, and dropping every
  bridge the moment auth is enabled would silently degrade the graph for everyone), while an
  edge whose evidence row is **missing** is dropped — a visibility filter must fail closed.
  **`/learn` gained ownership** (`note_source`): a taught fact lands in `notes:<user>`, owned
  and private, unless `share=true` puts it in the historic `notes:user-taught` commons. A
  personal note is a separate **source**, not a document flag, so it inherits filtering, graph
  scoping, cleanup and its own plate for free. Anonymous/open-mode teaching keeps the commons
  bucket, so every note taught before this stays exactly as readable as it was. Surfaces:
  `share` on `POST /api/learn`, `--share` on `qj learn`, a `share` argument on the agent's
  `remember` tool (prompt-instructed to pass it only on an explicit request), and a
  "Share with everyone" checkbox on the thumbs-down feedback modal.
  Tests: `tests/test_knowledge_scopes.py` (written as leak tests: the AND-ing, the
  fail-closed empty list, escalation via picking an unreadable source *or* document, every
  graph channel, the bridge-vs-dangling-edge split, note ownership, and an end-to-end
  two-user API test verified to FAIL with the filter disabled) + a Postgres parity test in
  `tests/test_pg_backend.py` **actually run against pgvector**, not statically reviewed.
  **`GET /api/sources` filters ingestion buckets too (2026-08-10).** It skipped the
  ownership check for any row with no `SourceConfig`, on a comment asserting that
  catalog-only rows "are commons" — true when it was written and false the moment a
  taught note could be private, so `Notes from ada` and its document count were listed
  to everyone. Retrieval and the graph were already filtered; this was the last
  enumeration surface, and it is the one the Rail renders.
  **The ingest-time merge guard (2026-08-10, Y1.8c).** Filtering decides what a person
  may READ; this decides what a private document may WRITE, which is the half filtering
  cannot undo — a merge, a rename or a bridge rewrites canonical graph state for the
  whole organisation and no read-side predicate puts it back. `catalog.is_private_source`
  is the ingest-side twin of `visible_source_ids` (owned AND not shared; an unknown or
  ownerless source is NOT private — the read side fails closed on a missing row, the
  write side deliberately does not, because withholding a document is cheap and
  reversible while refusing to merge is a permanent quality loss nobody would notice).
  `pipeline._persist_graph` then closes **three** paths, not one, because they are the
  same act through different doors: the entity resolver is skipped (no alias onto a
  canonical org entity), `upsert_entity(allow_rename=False)` makes the write insert-only
  (the rename rule is the quieter merge — a materially different name replaces an org
  entity's display name everywhere), and an extractor-supplied alias row is written only
  onto entities the same source minted (`entities.source_id`, which records the minting
  source and is never rewritten). `refresh_same_as_bridges` additionally excludes
  entities minted privately — a bridge is the one edge with **no evidence document**, so
  `_evidence_visible` keeps it visible to everyone and bridging a private-only entity
  would publish its name however well its documents are filtered. What is deliberately
  still allowed is ATTACHING: an exact-id assertion still becomes an edge, because a
  private note about a real service is the point of teaching one. The cost is a
  duplicate node in the owner's own view when their wording differs from the org's —
  visible only to them, and cleared by promoting the note.
  Not cached (ownership can change between two documents of one run, and it is a single
  indexed read against the N entity upserts that follow it).
- `quickjoiner/promotion.py` — **personal → organisation, by review (2026-08-10, Y1.8f).**
  Private-by-default was right and on its own a dead end: the useful half of what a joiner
  works out is exactly what the next joiner needs, and nothing carried it across.
  **A metadata flip, not a copy.** A `doc_id` is derived from `(source_id, uri)` at ingest
  and opaque forever after, so promotion rewrites which source owns the document and
  nothing else — same chunks, same vectors, same `evidence_doc_id` on every edge, same
  labels, same citations, no re-chunk and no re-embed. Three writes make it real
  (`catalog.move_document`, plus `store.move_document` on **both** backends, which rewrites
  the chunk rows' own `source_id` column and the FTS sidecar's), because retrieval filters
  on the chunk's copy — a document moved in one place and not the others would be
  retrievable through one leg and invisible through the other. The target is
  `promoted:org`, an ownerless commons bucket, which is what makes **every existing
  visibility predicate start including it with no read-path change at all**.
  Three steps, each with a reason: **request** (only the owner, only for a source only
  they can read — a document already public is refused rather than silently no-op'd,
  since a no-op that reads as success leaves the requester believing they published
  something); **queue** (`GET /api/promotions[/{doc_id}]` — a reviewer may open a document
  that is *still private*, a real but narrow exception scoped to `requested` state and to
  these endpoints only, sound because its author offered it; retrieval is untouched, so an
  offered note is still unanswerable to anyone but its author); **decide**. Approval also
  calls `catalog.rehome_document_entities`, releasing exactly the entities the promoted
  document cites from the merge guard's hold — scoped to that document, so a promotion
  cannot release the rest of a private bucket's graph.
  RBAC splits `promotions:request` (**viewer** — offering your own note is yours to do)
  from `promotions:review` (**editor**), for the same reason the skills caps are split:
  the review capability also carries that read exception, so folding it into `memory:write`
  would have widened something. Surfaces: the four routes above, `qj promotions
  offer|mine|queue|decide`, an **offer to org** chip on each document of a private source
  in the document browser (and a new **Your private notes** rail group, without which that
  browser was unreachable — the rail lists only *configured* connectors, so a notes bucket
  had no row at all), and a **Offered to the organisation** review panel in Settings.
  `confidence.classify_evidence` gained a 4th `source_id` argument and a **`personal-note`**
  class at 0.28 — below every org-visible document class because nobody else has ever seen
  it, above `meeting-notes` because a taught fact is a deliberate statement where a dated
  journal page is an incidental record. Provenance is checked before shape, and promotion
  *lifts* the discount by moving the note out of the personal bucket rather than re-scoring
  it, which is the incentive the flywheel wants. Deliberately NOT applied to a private
  *connector's* documents — an org system one person can reach is a different thing from a
  personal jotting, and a pure function cannot tell without an ownership lookup.
  ⚠ Stated loose end: promotion releases those entities but does not re-run entity
  resolution, so a promoted note that spells a service differently keeps its own node until
  the next `qj regraph` or re-sync — a missing merge, never a wrong one. And a viewer cannot
  currently teach a note at all (`POST /api/learn` is `memory:write`), so today the flywheel
  starts at editor.
- `quickjoiner/api/` — FastAPI. **OpenAPI/Swagger is grouped + documented** (2026-07-20): the app
  carries a top-level `description` + `openapi_tags`, and every route decorator has `tags=[...]` +
  a plain-English `summary=` (the HTML `/` route is `include_in_schema=False`). **87 operations
  across 12 tag groups** (Status / Authentication / Connectors / Sync & ingestion / Ask & search /
  Knowledge graph / Knowledge gaps / Sessions & projects / Briefs & repo docs / Settings / Skills /
  Webhooks) — counted from `app.openapi()`, not from memory; the previously-stated "43 across 11"
  had been stale for many endpoints. Interactive
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
  endpoint was relaxed to serve manager-only jobs (reset, drain, or a just-deleted connector's
  cleanup) — auth-gated, not requiring a configured source;
  **`GET /api/graph/pending`** (`graph:read`) — how many ingested documents still have unmined
  relationships, by source, plus whether extraction is even enabled; **`POST /api/graph/drain
  [?source_id=]`** (`sync:run`) starts the drain job described in the sync-manager bullet, 409
  while another job runs or when `graph.extract_triples` is off;
  **`POST /api/uploads`** (multipart, `memory:write`) — off-hand document uploads (chat drag-drop /
  attach button / API): each file is `save_upload`-ed into `<workspace>/uploads/` and ingested into
  the rolling uploads source via `ingest.extract`, so it's cited memory immediately; returns a
  per-file result (title/ingested/reason). **`POST /api/uploads/local`** (`{path}` JSON,
  `memory:write`) — the `/qj`-friendly variant: ingests a server-readable file path (copied into the
  uploads folder first). The multipart `/api/uploads` is refused by the `qj_api` control tool (it
  can only send JSON) with a hint to use `/api/uploads/local` or the attach button;
  `GET/PATCH /api/settings` — the whole `Config` (llm/embedding/retrieval/chat/**graph**) as a
  tunable dict; `GET /api/settings/defaults` — the same groups built from **freshly-constructed
  config models** (never the saved config, or every field would read as default forever), so the
  Settings drawer can mark which fields are still stock without hardcoding the values.
  **A settings write re-derives what was built FROM config** (`AppContext.apply_config()`,
  2026-07-30, user-reported): `build_context` wires the store and the ingest pipeline once at
  process start, so persisting alone left the *running* pipeline holding the extractors it was
  born with — turning on `graph.extract_triples` saved fine, `GET /api/settings` read back
  `true`, and a clean re-sync then ingested with no triple extractor, producing only
  deterministic edges (observed live: saved config `extract_triples: true` while
  `/api/graph/pending` reported `extraction_enabled: false`). Query-side retrieval knobs the
  **store object** holds (`hybrid`, `rrf_k`, `candidate_multiplier`, `ann_*`, the reranker) had
  the identical trap, while everything read per-call from `ctx.config` (`min_score`, `top_k`,
  `graph_expansion`, `alias_expansion` — `build_agent` passes `config.retrieval` fresh each
  turn) was always live, which is exactly what made the bug hard to see. `apply_config` rebuilds
  the pipeline via the shared `_build_pipeline` helper and pushes retrieval into the existing
  store through `set_retrieval(retrieval, reranker)` (both backends + the `StoreBackend`
  Protocol) rather than reopening LanceDB + the FTS sidecar under a possibly-running sync; the
  embedder — the one expensive object, `FastEmbedEmbedder.__init__` loads the ONNX model
  eagerly — is only rebuilt when `config.embedding` actually changed, tracked by
  `AppContext.embedding_signature`, and that case does build a fresh store since the vectors
  themselves change. Note this fixes *future* ingests only: documents already ingested while the
  extractor was absent were never queued in `graph_pending` (nothing deferred them), so they need
  a clean re-sync — `drain-graph` has nothing to drain for them. Regression-tested in
  `tests/test_api.py::test_settings_change_reaches_the_live_pipeline_not_just_the_saved_config`
  (verified to fail without the call). `POST /api/llm/test` probes the provider with a one-token round-trip, accepting
  optional unsaved `llm` overrides so the Settings drawer can verify a proxy/model before saving,
  never persisting) + `hooks.py` (verified `POST /hooks/{source}` push ingestion — 2026-07-26:
  gained a `?token=<webhook_secret>` query-param scheme alongside the header-based ones, for a
  sender that can only configure a bare callback URL with no custom headers or signing at all —
  Azure DevOps Service Hooks and Octopus subscriptions both fall in that bucket, and it's the
  only scheme either can actually satisfy; see the module docstring for all four schemes.
  `receive_hook` also now checks `connector.wants_resync(payload)` before the normal
  handle_event→ingest path: a connector whose webhook can't carry the actual content it needs
  to learn — the git-clone connector's push events carry commit metadata, never file contents —
  returns `True` there instead, and the router kicks off a real background `sync()` job via the
  same `SyncManager.start()` "Sync now" already uses, rather than trying to force a webhook
  payload into `handle_event`). Bearer token via
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
  `/api/learn` (`onLearned` refreshes status/gaps); streaming caret),
  **`components/ThinkingTrace.tsx` — the reasoning trace as a step-by-step timeline
  (2026-08-08, user request, modelled on Confluence ROVO's):** the run genuinely IS a
  sequence — think, call a tool, read what came back, think again — and the SSE stream
  delivers those events in that order, but the old view threw the ordering away, putting
  reasoning in one `<pre>` blob and tool calls in a separate row of pills that showed only
  function names. `Msg.trace: TraceStep[]` is now the canonical ordered record (`thought` |
  `action` | `note`), built by three pure reducers (`appendThought` merges consecutive
  reasoning deltas so a run of them stays ONE step; `startAction`; `finishAction`, which
  pairs on `id` and falls back to the trailing unfinished call of the same name so a
  provider that rewrites ids can't leave a step spinning forever). Each step is a row on a
  vertical rail — icon, bold title, one-line detail, chevron to expand into the full
  arguments and the result preview. **Two honesty rules govern the file**, since a trace
  that embellishes is worse than none: thought titles are the model's OWN segmentation
  (`splitThought` splits on its `**bold**`/`##` markers — which is what makes our output
  look like ROVO's when the model provides them; a blob with no markers stays one step
  titled "Reasoning", never an invented summary), and a result preview always states its
  true size. `actionLabel` hand-writes phrasing for the ~13 built-in tools and *derives* it
  for the ~60 connector live tools from their `<system>_<verb>_<noun>` naming
  (`gitlab_list_merge_requests` → "Listing merge requests in GitLab"), so a tool this file
  has never heard of still reads as English rather than a bare symbol. The `/scrape` crawl
  log feeds the same timeline via `pushNote` (one row per progress line) instead of the old
  concatenated blob. Note the rail line uses **`bg-hair`, not `bg-border`** — at 0.07 alpha
  the border token is invisible against the panel fill, and a timeline whose connecting line
  can't be seen is just a list.
  **`CiteBook` resolves a cited label to the page behind it (2026-08-08, same request):**
  constructed with the turn's `sources`, it exposes `source(ref)` / `href(ref)` /
  `entries()`, so a citation superscript becomes a real anchor and the sources modal leads
  with the readable title (linked, with its excerpt and uri beneath) instead of a raw ref
  string. **Matching is normalized EQUALITY only** (case, typographic dashes, whitespace
  runs, surrounding punctuation) — models paraphrase titles freely and fuzzy matching would
  silently point a citation at a document the answer was never about; an unlinked citation
  is the honest failure, and the repo's own rule is that a wrong link is worse than none.
  A ref that is itself a URL still links to itself, preserving the behaviour that already
  worked. `parts(ref)` handles the model putting several sources in one bracket
  ("[A, B]"), which resolves to nothing as a whole: it splits on `,`/`;` **only when EVERY
  part resolves** to a retrieved source — so a real title containing a comma ("Jan 6, 2026
  retro") cannot be torn apart, and a bracket naming a page plus something we never
  retrieved (a Confluence *space*, observed live) stays honestly unlinked rather than
  half-linked. The chip links only when exactly one source resolves (one destination, one
  link); the modal and the export list every named source.
  Plus a **Download
  conversation** button at the top of the chat pane exporting the whole open conversation to
  one markdown file — questions, answers, candidates, and each turn's **reasoning timeline as
  a collapsible GFM `<details>` block** (numbered steps, each tool's arguments as a bullet
  list and its result in a `safeFence`d block stating the true size — `traceToMarkdown`
  derives from the SAME `Msg.trace`, so the file and the screen cannot drift apart), with a
  per-turn sources list whose `[n]`
  numbering is harvested by re-running the same `CiteBook`/`renderMarkdown` pass the UI renders
  with (so exported numbers cannot drift from the inline superscripts). **Client-side only, and
  honest about one limit:** thinking traces are never persisted server-side
  (`GET /api/sessions/{id}` returns only `role`/`content`), so a reloaded page or a conversation
  reopened from the Rail exports its Q&A **without** traces — only turns generated live in the
  current tab carry them. Backend persistence of the trace is a genuine follow-up, not built.
  `Rail` (compact fixed-top /
  independently-scrolling conversations / pinned-bottom systems layout with `min-h-0`; per-row
  **delete** on hover + **Clear all** in the Conversations header → `DELETE /api/sessions[/{id}]`,
  clear-all confirmed + project-scoped to what's shown; each **Connected systems** row shows a
  pulsing "syncing…" state and re-opens that job's live log when it has one. **Connected systems are
  grouped by connector type** (2026-07-23): a type with ≥2 members collapses into one header
  (default collapsed, biggest group first) showing its count + a `TYPE_LABELS` friendly name
  (`GitLab`, `Azure DevOps`, `Git repositories`, …) and a pulse dot if any member is syncing; a lone
  system stays a plain row — keeps a workspace with many sources scannable. `renderSystemRow(s, nested?)`
  is the shared row; `expandedGroups` is per-type toggle state), `TopBar` (hosts the
  **`NotificationsMenu`** bell before Settings), `Composer` (also a **per-question attachment
  drop-zone**: a paperclip attach button + drag-and-drop **stage** files as context for the NEXT
  question — `App.pendingFiles` shows removable chips; on send they upload via
  `api.uploadChatAttachments` → `POST /api/chat/attachments` (ephemeral context, NOT memory) and
  their metadata stamps the user message. This is deliberately NOT the Uploads connector —
  attaching in chat never ingests into learned memory; that stays an explicit `/qj`/API action.
  `Chat.AttachmentChips` renders each attachment beneath its question: a download button, or —
  once the 7-day retention sweep deleted it — a struck-through name + warning icon + a
  when-deleted tooltip),
  `EmptyState`, `SettingsDrawer` (account + workspace settings + connector plates/forms; **connector
  plates are grouped by type** (2026-07-23, `ConnectorGroup`) exactly like the Rail — a type with ≥2
  members collapses into one header (default collapsed, biggest first) showing a count + syncing dot,
  a lone system stays a flat plate; friendly labels come from the fetched connector catalog
  (`types[].label`); the
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
  **Knowledge-graph canvas — `components/GraphView.tsx` + `components/graph/` (rebuilt
  2026-07-24):** the view used to snap an SVG `viewBox` straight into React state on every
  wheel tick and pointer move, and node radii were **world** units — so interaction stepped
  rather than moved, and any wide auto-fit (a 400-edge overview, or "expand neighbors" merging
  a few hundred entities) painted the whole graph as one-pixel dust. Rebuilt on three pure
  layers with one render loop:
  · **`graph/camera.ts`** — a continuous `{x, y, k}` camera (screen = world·k + xy) that the
  loop *eases* toward a target (`approach`, frame-rate-independent exponential; scale
  interpolated **geometrically** because that's how zoom is perceived), with pointer-throw
  **fling inertia** (`blendVelocity` while dragging → `decayVelocity` after release, so a
  flick coasts and settles). Wheel/pinch zoom is **anchored on the cursor** (`zoomAround`).
  **`markScale(k)`** is the fix for "everything becomes tiny": painted mark size follows
  `k^0.35` clamped to [0.62, 1.85], returned as the world-space counter-scale each node
  carries — so a mark is ~8–18px at *any* zoom instead of vanishing. Zoom-out is floored at
  0.4× the everything-fits scale (`minZoom`), so the graph can't be lost in an empty field.
  · **`graph/simulation.ts`** — a **persistent** d3-force layout (was one-shot: 300 ticks then
  a frozen snapshot, so every merge teleported nodes). Node objects and velocities survive a
  `setData`, new nodes are seeded on an already-placed **anchor neighbour** (golden-angle
  spiral) so an expansion grows outward from what was expanded, and nodes are **draggable**
  live (release un-pins and they rejoin the sim). Weak `forceX/forceY` instead of `forceCenter`
  — this graph is mostly disconnected components and `forceCenter` translates the whole system
  every tick. The loop ticks it; d3's own timer is never started.
  · **`graph/lod.ts`** — map-tile level of detail (the answer to "too much data"): **cull** to
  the visible world rect + half-screen overscan, **budget** by importance (pinned = selected /
  its neighbours / path hops, then by degree), and **re-tile only when the camera lands on a
  new quantised tile** — so panning and zooming are one transform write, not a re-render of
  thousands of elements. What was elided is *stated* ("N in view" in the toolbar), never
  silently dropped. **Two elisions, two statements** (2026-08-04): the client's LOD says
  what it isn't *drawing*, and the toolbar additionally says what the **server** never sent
  ("· sample of 109,003", from the `totals`/`truncated` `/api/graph` now returns). They
  survive an "expand neighbours" merge — adding to the view doesn't make it the whole graph,
  so the denominator it is a sample OF is still the right one.
  Consequently **React owns what exists** (which nodes/edges/labels, selection, filters) and
  **the loop owns where it's drawn**: node transforms and edge endpoints are written
  imperatively and never appear in JSX, so a re-render can't fight the animation. Edge widths
  use `vector-effect="non-scaling-stroke"`; labels are a zoom band toggled with one CSS class
  (`.graph-labels-on`, `LABEL_MIN_SCALE`) so they cross-fade with zero React work; the loop
  stops itself when camera, fling and layout are all at rest (an idle graph costs no frames)
  and every interaction calls `wake()`. Panels live in `graph/panels.tsx`, shared tokens in
  `graph/theme.ts`. Two interaction bugs fixed in the same pass, both found in the browser:
  (1) a press on a node used to pin+reheat the layout immediately, so the graph shifted
  between the two clicks of a double-click — pinning now waits for `NODE_DRAG_SLOP` (4px), and
  double-click expands the node the pointer was **pressed** on rather than `e.target` (a
  dblclick is dispatched to the nearest common ancestor of its two clicks, which degrades to
  the `<svg>` the moment anything moves); (2) the node inspector was a flex **sibling**, so
  selecting a node shrank the canvas and lurched the whole graph sideways — it now floats over
  the canvas (the minimap slides clear of it).
- `quickjoiner/export.py` — markdown → md/html/csv/pptx (`--format` on `qj ask` / `qj brief`);
  SSE chat events: `thinking` / `delta` / `tool_call` / `tool_result` / `sources` / `candidates` /
  `answer` / `error` / `done`.
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
  **`is_refusal` normalizes typographic apostrophes before matching (2026-07-27):** found running
  the plan-05 agent-layer matrix live against the AppRiver corpus (gpt-oss-120b via litellm) — the
  model renders the mandated refusal phrase with a mix of straight and curly apostrophes (`haven't`
  vs `haven't`) even within one eval run, and the old exact-substring check silently miscounted 3 of
  4 refusal cases as false answers. Now reuses `ingest.normalize.normalize_query`'s char-fold table
  (same fix pattern as doc-text normalization) before matching. This also unmasked two genuine
  false-refusal cases that had been hiding behind the bug (see the plan-05 status note below).
  Regression-tested (`test_is_refusal_matches_typographic_apostrophe`).
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
- `quickjoiner/bench/harness.py` — **`qj bench`: the latency/cost harness (2026-07-31,
  AI_ROADMAP S1).** The speed/cost twin of the eval harness and deliberately its mirror
  image — a YAML pack of queries (an **eval set works too**: its `cases[].question` list
  IS a query pack), a JSON report under `<workspace>/bench/`, and a `--compare` that exits
  non-zero on regression. The house rule it serves: no speed work merges without a
  before/after bench table, exactly as no quality work merges without `qj eval --compare`.
  Four layers: **retrieval** (no LLM, always) — per-stage p50/p95 for embed-query, dense,
  sparse, fuse, sparse-rescore, rerank, gate, plus alias expansion and graph expansion,
  which sit *around* `store.search` on the real `search_memory` path; **embed** (no LLM) —
  embedder chunks/sec at a realistic batch, the ingest bottleneck S6 must beat; **sync**
  (no LLM, always) — see below; **agent**
  (`--agent`, needs an LLM) — time to first token, full-answer time, model rounds, tool
  calls, and tokens per answer incl. cache reads.
  **Ingest throughput is READ, not run** (`run_sync_bench`, 2026-08-04, closing S1's
  remainder): docs/min overall and per connector, computed from the runs already recorded in
  `sync_events` over a `--sync-days` window (default 7). Performing a sync would measure the
  remote's mood on the day and a synthetic one would measure a fixture, while the honest
  number for a real pull was already on disk. **Stopped and errored runs count** — they
  ingested real documents over a real duration, and excluding them would systematically drop
  exactly the long crawls whose throughput matters most, so `SyncManager` now records
  `stats["ingested"]` on those paths too (it previously wrote stats only on success, leaving a
  partial run as a blank history row). Runs under a second are skipped as too short to divide
  by, and a window with **no** finished run reports that fact rather than a confident zero.
  Costs one indexed query, so it is always in the report.
  **Measurement seam:** `store.search(trace=SearchTrace())` (both backends + the
  `StoreBackend` Protocol). Opt-in and caller-supplied — nothing in the product passes
  one, so benchmarking cannot change what production does; `trace=None` gets a `NullTrace`
  whose stages are an empty `with` block (~1µs total against a multi-ms search), which
  keeps ONE search implementation rather than a traced fork of it. A stage entered twice
  accumulates (`_dense` runs again for the sparse rescore).
  **Token seam:** `ChatResult.usage` (`llm.base.TokenUsage`: prompt/completion/cached/
  cache_write) — each provider maps its own wire shape onto it (Anthropic
  input+cache_read/output/cache_creation, OpenAI-compatible prompt/completion +
  `prompt_tokens_details.cached_tokens`, Ollama `prompt_eval_count`/`eval_count`), and
  `OnboardingAgent.last_usage`/`last_rounds` sum it over a turn's rounds. Zeros mean "not
  reported", never "free". This is what S5's cost-delta measurement needed and never had.
  **Honesty built in:** warm-up runs are discarded (the ~80MB cross-encoder loads lazily on
  first `rank()`; folding that into a p50 would make any change that moved it look like a
  win), `samples` is reported beside every percentile, regressions are judged **relatively**
  (`COMPARE_TOLERANCE` 0.20 — +5ms means nothing at 500ms and everything at 8ms, unlike the
  eval harness's absolute 0.02 on rates), and a metric with fewer than
  `MIN_SAMPLES_FOR_GATE` (5) samples is reported with its delta but **never fails the gate**
  (a gate that fires on noise gets switched off, which costs more than it saves).
  **What it found on its first real run** (live 21,607-doc / 56,519-chunk workspace, 32
  queries × 3): total retrieval **3.08s p50** — nobody had ever measured the user-facing
  number — of which **rerank 61%** (1.89s) and **alias expansion 27%** (841ms). The second
  was a genuine defect, not a cost: `catalog.resolve_entity`'s `WHERE id = ? OR
  LOWER(name) = ?` had no index for the LOWER(name) branch, so every call **scanned all
  36,203 entities**, ~30 times per query (alias expansion slides 1–4-token windows) —
  while the code comment asserted "every lookup is an exact indexed hit so the extra pass
  is cheap". Fixed with an expression index (`idx_entities_lower_name`, in the shared
  `_MIGRATION_STATEMENTS` ⇒ both backends; the planner now reports MULTI-INDEX OR):
  `expand_query` **382ms → 1.88ms**, total **p50 3084→2186ms (−29%), p95 5862→2834ms
  (−52%)**, measured with `--compare`. Guarded by an EXPLAIN-based test, because every
  correctness test passed throughout and none of them could ever have caught it. The
  remaining 83% is the cross-encoder at **~77ms/candidate** on ~420-token chunks, scaling
  linearly (4→293ms, 12→879ms, 24→1843ms) — that is a `rerank_candidates` depth decision,
  i.e. **S4, since settled 2026-08-10**: the depth was measured rather than guessed and cut
  24 → 16 (see the retrieval bullet above), taking the query to **p50 1401ms** with rerank
  76% of it and no measured recall cost. Agent layer live (gpt-oss-120b via the
  litellm broker): first token 22.3s, full answer 32.7s, **22.4k tokens/answer** over 3
  rounds — and **zero cache reads**, the S5 verification that had been pending.
  Tests: `tests/test_bench.py`.
- `quickjoiner/scheduler.py` — APScheduler periodic syncs for sources with `sync_interval_minutes`,
  PLUS a standing `context-attachment-cleanup` sweep (every 6h + once at startup) that expires
  per-question chat attachments past `chat.context_retention_days`. Now **always** returns a running
  scheduler (the cleanup must run even with no interval-synced sources).
- `quickjoiner/chat_attachments.py` — per-question chat file attachments: **context for one
  question, NOT learned memory by default — with an explicit opt-in to make it memory**
  (2026-07-24, user request). `POST /api/chat/attachments/{id}/learn` promotes an
  already-uploaded attachment into the rolling Uploads connector (re-saves the stored bytes
  via `save_upload` + the shared `_ingest_upload_path`), so the same file both answers THIS
  question and becomes cited memory; the attachment stays downloadable. 410 (not 404) once
  the retention sweep removed the bytes — "it existed and is gone, re-attach it". Reached
  two ways: the composer's **Learning permanently** toggle beside the staged chips (default
  "Ask only", resets after every send so it can never silently persist), and by **asking** —
  `build_context_block` now emits each file's attachment id and tells the agent to call
  `qj_api POST /api/chat/attachments/<id>/learn` when the user asks to learn/remember/save
  it. Both were reported live: an attached deck + "learn from this document" got "I'll need
  the content", and `ingest <local path>` got "I don't have direct access to files on your
  computer" — the latter because `qj_api`'s description listed connectors/syncs/settings but
  never mentioned **ingesting documents**, so the model never connected the request to an API
  it already had. That description now names both `/api/uploads/local` and the attachment
  learn path explicitly, and says never to claim it cannot read the machine's files.
  The default is unchanged: nothing is ingested unless asked (never embedded/indexed/graphed, and never folded into the Uploads
  connector unless the user explicitly asks). `store_attachment` extracts text (via `ingest/extract`)
  to `<workspace>/context/<id>/`, `build_context_block` injects it into a chat turn's system prompt
  (cited as `[file: <name>]`), `resolve_message_attachments` overlays a stored message's attachments
  with their current deleted/downloadable state, and `cleanup_expired` (the scheduler sweep) removes
  the bytes after the retention window while keeping the catalog row (`deleted_at`) so history still
  shows the name + deletion time. Catalog table `context_attachments`. See the chat-attachments
  status entry for the endpoints + UI.
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
  (from the search API's `total`), `octopus` (project-list pull paged with `%` from the response's
  `TotalResults` via `_paged(stage=…)` — checkpoints between pages; then per-project releases), `confluence` (per space, via a one-call
  CQL `totalSize` preflight — `_space_page_count`; unscoped all-spaces pull ⇒ no total ⇒ shimmer).
  **Confluence body extraction uses `body.view`, not `body.storage`** (2026-07-23): `page_document`
  reads the **server-rendered** `body.view` HTML (falling back to `body.storage` when only that is
  present, e.g. a webhook payload). Storage format leaves user-mentions as empty
  `<ac:link><ri:user account-id=…/></ac:link>` elements and macros as `<ac:structured-macro>` shells
  that `get_text()` drops — so a page listing its team via @mentions ingested with the member **names
  blank** (found live debugging "composition of team Caffeine": the Members table came through as
  roles with no people, while a sibling charter that typed plain-text names extracted fine). `body.view`
  renders mentions to display names, expands macros, and renders tables. The `sync`/webhook `expand`
  is `body.view,version`. **Needs a Confluence re-sync** to re-ingest existing pages (hash changes ⇒
  re-embed). Tests: `test_confluence_view_body_captures_rendered_user_mentions` +
  `test_confluence_page_document_falls_back_to_storage`.
  **Confluence renders its tables through `render_html_table` too (2026-08-05,
  user-reported).** `body.view` renders a table into real HTML, but `page_document` then
  flattened it with `get_text()` — so the 2026-07-31 table-fidelity work reached every
  source EXCEPT Confluence, and a Team Charter's roster arrived as a vertical stream of
  cells with nothing tying a role to the person holding it. The blank-cell failure is the
  visible one: a charter whose "Product Owner" row has no name rendered as
  `Product Owner / Delivery Manager / Etheria Hill`, so the next person absorbs both roles
  and the honest answer to "who is on Caffeine" was **one name out of eight** — exactly
  what the user reported. Now leaf tables are rendered to pipe rows before flattening,
  same two shape rules as the extractor. **Needs a Confluence re-sync** (text changes ⇒
  hash changes ⇒ re-embed). Test:
  `test_confluence_renders_tables_as_rows_so_a_roster_stays_readable`.
  NOTE: hyperlinks in the body (inter-page,
  and links to GitLab/TFS) are still stripped to text and are **not** turned into graph edges — the
  cross-source link-graph is a designed-not-built enhancement (see below / `docs/KNOWLEDGE_GRAPH.md`).
  **Confluence batch prefetch** (2026-07-22): `sync` prefetches the NEXT page batch on a worker
  thread (`_fetch_page_batch`, `ThreadPoolExecutor(max_workers=1)`) while the pipeline parses+embeds
  the current one, so the heavy `body.view` network round-trip overlaps the downstream work
  instead of being a serial gap between batches; `next_start` is always derived from the actual
  result count (never guessed), so pagination stays correct and `_checkpoint()` still lands between
  batches. Tests: the existing confluence paging/percent tests in `tests/test_connectors.py` cover it.
  **Phase label only** (UI shows a live shimmer, no fake denominator) where the API gives no cheap
  count: `github`/`gitlab` list APIs. All of these also `_checkpoint()` between page/section fetches so a stop lands
  within a page. The logsearch-inventory connector relies on the universal between-document
  check — it finishes in seconds, so there is no long non-yielding stretch to instrument.
  **`web_scrape` IS instrumented (2026-07-30)** — the earlier "it yields per page, so nothing to
  do" reasoning was wrong for the browser path: one `page.goto` + networkidle can take tens of
  seconds, so a Stop waited for the whole page and the UI showed a frozen "syncing…" for the
  entire crawl. `_crawl` now `_checkpoint()`s and `_stage()`s per URL; the denominator is the
  **known frontier** (`visited + queued`, capped at `max_pages`) rather than the raw page budget
  — it grows as links are discovered and converges on the true count as the queue drains, which
  is honest about a crawl whose size genuinely isn't known up front (a fixed `max_pages`
  denominator would sit at 4% and then jump to done on a 12-page site).
  `SyncJob` gains `phase`/`phase_done`/`phase_total` + `percent()` (surfaced in every
  `summary()`, so `/api/syncs` + `/api/notifications` carry them).
  **Self-healing network auto-retry (`_pull_with_auto_retry`/`_enter_network_hold`, state
  `retrying`, 2026-07-21):** a sync that dies mid-pull on a **transient network** failure no
  longer fails the run — it auto-pauses and retries itself. A raised exception kills the
  connector's generator (a Python generator can't resume past a raise), so a retry re-invokes
  `connector.sync(state)` from the **un-advanced watermark**; idempotent hash-dedupe skips the docs
  the failed attempt already committed (full-refresh connectors re-walk but re-embed nothing
  unchanged). Backoff **doubles from 20s** (`_RETRY_INITIAL_BACKOFF`) and it keeps retrying for up
  to **1 hour** (`_RETRY_MAX_WINDOW`) after the first failure; each wait is `job.cancel.wait()`, so a
  manual **Stop cancels the countdown at once**. Once the budget is spent the job parks in a
  genuine **paused** state (`_enter_network_hold` — thread blocked on `SyncJob.gate`, watermark
  never advanced) with clear logs; a manual **Resume restarts the whole budget** and re-attempts,
  **Stop** abandons it. Classification is `connectors.util.is_transient_network_error` — httpx
  transport errors, a persisted 429/5xx (`HTTPStatusError` after the HTTP layer's own 4-attempt
  retry in `_request_with_retry` was exhausted), and raw socket/OS failures incl. Windows
  `WinError 10060`. Only network errors retry: **auth/config/code errors (e.g. a 401 or a
  `ValueError`) fail immediately** as before, since they'd fail identically however long we wait.
  This composes with the existing per-call HTTP retry (that smooths sub-~7s blips; this rides out a
  sustained outage). The `retrying` state joins `running`/`paused`/`stopping` everywhere a job is
  counted "in flight" (`is_running`/`active_sources`/`recent`/`subscribe`/`stop`) and is rendered
  in the rail pill, connector plate, bell menu, history and log modal (gold "retrying…", Stop stays
  available). Tests: `tests/test_sync_manager.py` (classifier split, retry-then-recover, non-network
  fail-fast, budget→resumable-pause, stop-during-hold, stop-interrupts-backoff). Caveat: like all
  the cooperative control, this only governs syncs started **after** the current process — a run in
  an old server process isn't affected (a *paused* run is the exception — it's durable, see next).
  **Durable pause across a restart (`revive_paused`/`_resume_cold`, `SyncJob.cold`, 2026-07-21):**
  pause→resume now survives the worker being killed / the laptop being closed in between. The
  durable record is the existing `sync_events` row — a paused job persists as `state="paused",
  ended_at IS NULL` (written by `pause`/`_enter_network_hold` via `_record`), and
  `prune_sync_events` was hardened to **never drop an unfinished row** so a days-long pause outlives
  the 7-day retention window. At API startup `app.py` calls `syncs.revive_paused()`, which reads
  `catalog.list_unfinished_syncs()` and, for each `paused` sync whose connector is still configured,
  re-attaches a **cold** `SyncJob` (state `paused`, no worker thread) that owns the source; every
  other unfinished row (a run that died mid-flight, or a paused one whose config is gone) is
  finalized as `interrupted` so it stops re-appearing. A killed process loses the in-memory
  generator, so a cold job can't literally continue — `resume()` detects `cold` and calls
  `_resume_cold`, which spawns a fresh worker that re-runs `connector.sync(state)` from the
  un-advanced watermark (`clean=False` ⇒ never re-purges; idempotent dedupe skips already-ingested
  docs) — the same re-pull model as the network auto-retry. It does **not** auto-resume: the pause
  was deliberate, so it waits for the user. `stop()` on a cold job finalizes it directly (no worker
  to signal, so it can't wedge in `stopping`). `recent()` also unions in-memory active/paused jobs
  that predate the feed window, so a long-dormant revived pause still surfaces in the UI. A cold job
  presents as an ordinary `paused` job, so the rail/plate/bell/log-modal render it and its
  Resume/Stop with no UI change. Tests: `tests/test_sync_manager.py` (revive-as-cold, cold-resume
  re-pulls + completes, cold-stop finalizes, dead-running→interrupted, config-gone→interrupted) +
  `tests/test_catalog.py` (prune keeps unfinished, `list_unfinished_syncs`).
  **Drain jobs** (`start_drain(source_id=None)`, `SyncJob.kind` = `drain`, sentinel source
  `"graph relationships"`): mine relationships for documents whose deferred graph work never
  landed — see the `ingest/pipeline.py` `drain_pending_graph` note above for what it does and how
  faithfully. It is a job rather than a click because on a real corpus it is thousands of LLM
  calls; it therefore streams logs, reports `graph relationships (done/total)`, and pauses/stops
  like a sync (a stop leaves the rest queued, so the UI offers a plain Stop with no
  "clean up partial data?" prompt — there is no partial data, only unfinished queue).
  Mutually exclusive with every other job in both directions, since it writes edges across
  sources.
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
  makes **Name editable only before the first sync** (0 documents), otherwise disabled with the
  reason — identity: `source_id` = `type:name`, so every doc id, vector, graph node, watermark and
  webhook URL derives from it; with nothing ingested yet a rename is just a config move (PATCH
  `{name}`, guarded server-side on doc-count == 0 + no running job + name uniqueness; `save_config`
  reconciles the `sources` row and the old watermark is cleared), but after the first sync it would
  orphan the lot.
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

- **Grounding threshold**: `retrieval.min_score = 0.64`. **FIXED 2026-07-29 (Plan 05 /
  PRIORITIES #0, was a known defect):** above `retrieval.ann_min_rows` (4000),
  `ensure_ann_index()` builds LanceDB's default **IVF_PQ** index, which is product-quantized and
  lossy enough to matter. It has **two observed faces**, both fixed by the same change: scores
  wrong by 0.25–0.45 cosine on the *same* top chunk (measured 2026-07-28 on `eval-c3`;
  `nprobes` doesn't help), and — re-measured 2026-07-30 on the live 56,401-chunk `default`
  workspace — the right chunks **missing from the results entirely**, with accurate-looking
  scores on the worse chunks it substitutes (**recall@5 vs exact: 45% → 100%**, top-1 9/12 →
  12/12). The second face is the more dangerous one: nothing in the numbers reveals it.
  `_dense()` now sets `refine_factor` (`RetrievalConfig.ann_refine_factor`,
  default 10) to re-rank ANN candidates against their un-quantized vectors — restores exact
  results for +1.2ms/query measured (still *faster* than brute force: 17.6ms vs 21.3ms p50),
  regression-tested in `tests/test_retrieval.py`
  (`test_ann_refine_factor_restores_exact_scores_above_min_rows`, crosses `ann_min_rows` for real,
  which no prior test did). Small workspaces (< 4000 chunks) search exactly and were never
  affected. **The coupled retune**: with true cosines the whole score distribution shifts up, and
  an expanded 12-case refusal set (was 4, now cleared `CALIBRATION_MIN_CASES`) found refusal
  near-misses (0.62–0.78) and real answerable hits (0.66–0.87) overlap enough that **no threshold
  cleanly separates them** — `qj eval --calibrate`'s own max-margin pick was 0.72 (refusal_accuracy
  0.917, but costs 3–4 of 20 answerable cases their grounding). Shipped **0.64** instead, a
  deliberately conservative choice (user call, not the calibrator's own recommendation): zero
  measured answerable-recall cost on the expanded eval set, while still gating 3 of 12 refusal
  near-misses that leaked at the old 0.55 (up from 0). **This retune reaches EXISTING workspaces
  too as of 2026-07-31** — it did not when it shipped, and the live corpus ran 0.55 for two days
  afterwards; see the sparse-config-persistence bullet under `memory/` for the fix and the one
  step (`SUPERSEDED_DEFAULTS` + `DEFAULTS_EPOCH`) a future retune must take. Retune again if the embedding model changes,
  `embedding.instruct` is toggled, or the eval set grows enough to change the overlap picture — see
  `docs/plans/05-eval-on-connected-org.md` for the full sweep across candidate thresholds.
  Borderline hits are still passed to the LLM with scores; the prompt makes the final relevance
  judgment, which is why agent-layer refusal_accuracy has historically stayed at 1.0 even when the
  retrieval-layer proxy leaks — the retrieval threshold is a coarse pre-filter, not the only line of
  defense. **Standing gap** (PRIORITIES #33, unresolved by this fix): the overlap itself means
  threshold tuning alone has a ceiling — the embedding fine-tune question is now framed as a
  refusal-discrimination problem, not a recall problem.
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
- **Every API change is a UI+API change until proven otherwise.** Whenever an endpoint, request/
  response shape, or API-served catalog (e.g. `FORM_SPECS` → `/api/connectors/types`) changes,
  **scan the frontend for every consumer** (`frontend/src/api.ts` is the map; then the components
  and flows that call it — forms, wizard, modals, rail, bell) and scope the UI work into the SAME
  change: render the new capability, handle the new error/status codes, respect new constraints
  (disabled states with the reason, not silent absence). Then **verify in the browser** (Playwright
  against a scratch workspace) — "the UI is data-driven so it'll just appear" is a hypothesis, not
  a verification; it also misses that the *running server process* must be restarted to serve
  changed server-side data. A backend change whose UI half is unverified is an incomplete change.
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

- Rename a connector before its first sync (2026-07-20, user request). The name keys everything a
  connector ingests (`source_id = type:name`), so it was always fixed — but with **0 documents**
  nothing is keyed to it yet, so a rename is safe and just a config move. `PATCH /api/connectors/
  {name}` now accepts `{name}`, guarded on doc-count == 0 + no running job + uniqueness (409
  otherwise); it clears the orphan watermark and `save_config` swaps the `sources` row.
  `EditConnectorModal` makes the Name field editable when `documents == 0`, disabled with the
  reason after. Tests: rename-before-sync / rejected-after-sync / name-taken in `test_api.py`.
  Verified live in Chrome (Fresh 0-doc editable + renamed via UI; Synced 23-doc locked). Follows
  the new API-docs house rule: Swagger summary + Postman/Bruno runbooks updated in the same change.

- Cross-source identity bridges shipped (2026-07-21, priorities item — roadmap #24 graduated):
  see the `memory/` bullet above for the full design. Includes the user-requested **`aka`
  ("also known as") connector option** on git/files (FORM_SPECS-driven, appears in the web
  forms automatically; comma-separated) — a declared alias is the identity claim name-matching
  can't discover (repo "Stevedore" aka "appriver.provisioning"). Suite: **489 passed** (+13
  in `tests/test_bridges.py`: pure guards, alias bridging, aka end-to-end via `upsert_source`,
  graph_path/graph_expand crossing, gc sweep, confidence cap), 12 skipped, pre-existing
  eval-yaml failure unchanged. Follow-ups same day (user): `aka` now carries the generic
  `lock_after_sync` FORM_SPECS flag — same editability rule as the connector name (settable at
  creation, 409 + disabled-with-reason after the first sync; unchanged values round-trip via
  `_norm_opt`) — and the UI was **browser-verified** this time (create form shows the field;
  editable at 0 docs; locked with hint at 23 docs). The initial miss ("data-driven, it'll just
  appear" — asserted, unverified, and the user's running server needed a restart to serve the new
  FORM_SPECS) prompted the new **UI+API scoping house rule** in Conventions. Follow-up: `aka` was
  initially only on git/files (the "repos" the user named); when the user hit a **Confluence**
  connector without it, extended to **all 13 connector types** — "also known as" is meaningful for
  any connected system (alias search/expansion for every source; bridges where the source is a
  bridgeable entity). Browser-verified on the Confluence edit modal specifically. NOTE (honest): the live before/after demo was not possible —
  the user's workspace had been **memory-reset** and not yet re-synced (0 entities; the earlier
  158-match measurement was pre-reset). Bridges compute automatically as re-syncs land, via the
  pipeline hook. AI_ROADMAP #24 moved to Shipped; PRIORITIES renumbered; KNOWLEDGE_GRAPH.md §5.
- Self-healing sync on network failure (2026-07-21, user request): before, a network error
  mid-ingest (past the HTTP layer's 4-attempt retry) failed the whole sync with no recovery, and
  a manual pause/resume couldn't rescue it — the failure had already killed the worker thread and
  the connector's generator (a generator can't resume past a raise). Now a **transient network**
  failure auto-pauses the job into a new `retrying` state, waits an **exponential backoff (20s →
  40s → 80s …)**, and re-runs `connector.sync(state)` from the un-advanced watermark (idempotent
  dedupe skips already-committed docs) for **up to 1 hour**; after that it parks in a resumable
  **paused** state with clear logs (manual Resume restarts the budget, Stop abandons). Only
  network-classified errors retry (`connectors.util.is_transient_network_error`); auth/config/code
  errors fail immediately as before. Wired through the whole job surface + UI (`retrying` pill,
  Stop stays live). See the `sync_manager` architecture bullet for the full design. Suite:
  **496 passed** (+7: 6 new sync-manager tests + the classifier test), 12 skipped, pre-existing
  eval-yaml failure unchanged. Frontend rebuilt (typecheck clean). Answered in the same session:
  *before* this change, a manual pause/resume after a network failure did **not** continue the sync
  and could not — the job was already terminally `error` with its thread gone; it was abandoned
  cleanly (watermark not advanced, committed docs intact), not corruptly.
- Heartbeat honesty during the graph drain (2026-07-21, user-reported): once a connector's
  document pull finished and the pipeline moved into the deferred graph-relationships drain,
  `job.ingested` stopped changing, so the 20s heartbeat kept logging `still syncing — N documents
  so far` with a frozen N — reading as "stuck repeating the last document" even though the drain
  (`graph relationships 56/3091`) was advancing (that progress fed the top bar but never the log
  stream). Fix: `SyncManager._progress_line` — when a phase is being reported (`job.phase`/
  `phase_done`/`phase_total`), the heartbeat leads with that phase + its progress/% (e.g.
  `10988 documents · graph relationships 56/3091 · 2%`), falling back to the doc count only when no
  phase is set. Pure + unit-tested (`test_heartbeat_reports_the_active_phase_not_a_frozen_doc_count`).
- Durable pause across a restart (2026-07-21, user request): a sync that was **paused** now
  survives the worker being killed / the laptop being closed between pause and resume. The paused
  `sync_events` row is the durable record (prune hardened to never drop an unfinished row); at API
  startup `SyncManager.revive_paused()` re-attaches each still-configured paused sync as a **cold**
  job that owns the source, and Resume re-runs the connector from the watermark (idempotent dedupe
  skips what's done — the same re-pull model as the network auto-retry, since a killed process
  loses the in-memory generator either way). Deliberately does not auto-resume; runs that died
  mid-flight are finalized `interrupted` (unchanged). No frontend change — a cold job presents as an
  ordinary `paused` job. See the `sync_manager` architecture bullet for the design. Suite: **502
  passed** (+6: revive/cold-resume/cold-stop/interrupted-finalize + catalog prune-keeps-unfinished),
  12 skipped, pre-existing eval-yaml failure unchanged.
- Natural-language self-control (`/qj`) + RBAC (2026-07-21, user request; **plan 08**): full
  control of QuickJoiner from chat via its OWN API, gated by a real role system so it's safe at
  100+ users. **Generic dispatch** — `qj_api`/`qj_api_reference` (`agent/control.py`) call the API
  in-process and are the single choke point for permissions; the API is a **permanent, non-ingested
  control connector** (type `quickjoiner`) that can't be deleted. **RBAC** (`rbac.py`, `role` column
  on `users`): admin / editor / viewer with capability + connector scoping, enforced BOTH in the
  control tool and at the HTTP layer (`_require`). New endpoints `GET /api/auth/users` +
  `PATCH /api/auth/users/{username}` (admin), `role` added to `POST /api/auth/users` and
  `GET /api/auth/status`. Frontend: `/connect` + `/learn` replaced by `/qj <plain language>`; a
  **People & access** admin panel (list users + role dropdowns + add-user-with-role), the control
  connector shown as a locked plate, and role-gated affordances (viewers/editors don't see the
  memory-reset danger zone or admin controls). Full design + the staged build: `docs/plans/08-…md`.
  Suite: **530 passed**, 12 skipped, pre-existing eval-yaml failure unchanged; frontend typecheck +
  build green. OPEN: live browser (Playwright) verification of the `/qj` + admin UI flows.
  (Verified live 2026-07-21 via curl + headless Playwright — see `docs/plans/08-…md`.)
- Parallel ingestion reads (2026-07-22, user request — "file reading is going very slowly"; then
  "apply it to the other paginated connectors too"): the connector-side slice of AI_ROADMAP **S6**.
  Two reusable primitives + per-connector wiring:
  - **`files.read_documents_parallel(items, fn, stage, workers)`** — bounded, in-order,
    memory-safe parallel map for slow per-item I/O (GIL-releasing disk/httpx). `files`/`git` use
    it to read their file trees concurrently (the dominant cost of a large repo, and effectively
    the *whole* cost of a re-sync — unchanged files are still read to hash them); **Octopus** uses
    it to fetch every project's releases **concurrently** (was 100s of serial per-project round-
    trips on a big space). `read_workers()` auto-scales, `QJ_READ_WORKERS` overrides (1 ⇒ old path).
  - **`connectors/util.prefetch_pages(fetch, cursor0, checkpoint)`** — 1-page-ahead prefetch for
    sequential paginated APIs: fetches the NEXT page on a worker thread while the caller
    parses+embeds the current one, so the network round-trip overlaps downstream work.
    `fetch(cursor) -> (items, next_cursor)`; `next_cursor=None` ends it. Wired into **jira**
    (startAt offset pages), **github** + **gitlab** (page-number MR/PR/issue lists), **azure_devops**
    (per-team work-item id-batches), and **confluence** (its own inline variant — `_fetch_page_batch`).
  Both are side-effect-free (nothing commits until the pipeline ingests, idempotently) and honor
  cooperative pause/stop + `%` via the connector's `_checkpoint`/`_stage` — integrity and stop
  latency unchanged, only the fetching parallelizes. Suite: **539 passed** (+9,
  `tests/test_parallel_read.py`), 12 skipped, pre-existing eval-yaml failure unchanged. Per-connector
  detail in the connectors architecture bullets above.
- Document ingestion (Word/PowerPoint/Excel/PDF/…) + rolling Uploads connector (2026-07-22, user
  request): ingest office/PDF documents into learned memory. **Core:** `ingest/extract.py` is the single text-extraction choke
  point (docx/pptx/xlsx/pdf parsers + text/markdown/json/html decode with HTML alt-text), funnelled
  to by the `files`/`git` connectors (which now read office/PDF too), the rolling uploads connector,
  and the upload endpoints. **Rolling connector:** `connectors/uploads.py` — one permanent
  singleton drop-box (`<workspace>/uploads/`, source `uploads:uploads`) that accumulates any file
  type instead of a connector-per-file; syncable/cleanable but un-deletable/un-renamable/
  un-duplicable. **UI (2026-07-23):** rendered by a dedicated locked `UploadsPlate` in
  `SettingsDrawer` (like `ControlPlate`) — friendly "Uploaded documents" name + lock + blurb, Sync
  and Clean-up actions only, **no delete / rename / share** (the earlier version reused the generic
  `ConnectorPlate` and wrongly showed a trash button + raw `uploads` slug for a permanent source);
  the Rail also labels the reserved `uploads`/`quickjoiner` singletons in friendly form.
  **Surfaces (explicit "learn this permanently"):** `POST /api/uploads` (multipart)
  and `POST /api/uploads/local` (`{path}` JSON, the `/qj` path — the multipart route is refused by
  `qj_api` with a hint). NB (2026-07-23): the **chat composer's attach button was later repurposed**
  to per-question *context* attachments (see the chat-attachments entry below), so drag-drop in chat
  no longer feeds this connector — memory ingestion via uploads is now only the explicit `/qj`/API
  path, per the user's "don't inject unless asked". Text
  extracted at ingest; **images are NOT read yet** — an `ImageHandler` seam threads through the
  extractors (embedded images already enumerated + handed to it), so the **vision roadmap item
  (`docs/plans/07-multimodal-derive-to-text.md`, AI_ROADMAP #23) plugs in with no extractor
  changes** — the two features are deliberately linked. New deps: python-docx/openpyxl/pypdf +
  python-multipart (python-pptx already present). Suite: **559 passed** (+20: `tests/test_extract.py`
  ×9, `tests/test_uploads.py` ×10, control-tool multipart guard ×1; auth tests updated to exclude
  the new singleton), 12 skipped, pre-existing eval-yaml failure unchanged; frontend typecheck +
  build green.
- Per-question chat file attachments — "ask about this file" (2026-07-23, user request): a distinct,
  ephemeral flow, kept **separate from the Uploads connector and learned memory**. `chat_attachments.py`
  (new): a file attached to a chat message is extracted to text (via `ingest/extract`) and stored under
  `<workspace>/context/<id>/` (original + `text.txt`) with a `context_attachments` catalog row — never
  embedded, indexed, graphed, or citable as memory. **Chat integration:** `ChatRequest.attachment_ids`;
  `build_context_block` injects the files' text into the turn's system prompt (capped by
  `chat.attachment_context_max_chars`, instructing the agent to use/cite them as `[file: <name>]` and
  NOT to save them unless asked), `agent.ask`'s user turn gets the attachment metadata stamped on it,
  and the attachments bind to the session. **Endpoints:** `POST /api/chat/attachments` (multipart,
  `chat:use`) and `GET /api/chat/attachments/{id}/download` (`chat:use`, **410 Gone** once expired).
  `GET /api/sessions/{id}` resolves each message's attachments to their CURRENT state (deleted or
  downloadable) at read time. **Retention:** `scheduler.py` now ALWAYS runs (a standing
  `context-attachment-cleanup` sweep every 6h + once at startup) — `chat_attachments.cleanup_expired`
  deletes the bytes of attachments older than `chat.context_retention_days` (default 7) but KEEPS the
  row (marked `deleted_at`) so history still shows the filename + when it went. **Frontend:** the
  Composer attach button/drag-drop now STAGE per-question files (removable chips) that upload on send;
  `Chat.AttachmentChips` renders them beneath the question — a download button, or a struck-through
  name + warning icon + when-deleted tooltip once swept. Config: `ChatConfig.attachment_context_max_chars`,
  `context_retention_days`. Suite: **566 passed** (+7 `tests/test_chat_attachments.py`; scheduler test
  updated to assert the always-on cleanup job), 12 skipped, pre-existing eval-yaml failure unchanged.
  Browser-verified via Playwright (stage chip → send → attachment renders under the question with a
  download link; Uploads connector stays 0 docs) + live uvicorn (upload/download/410/isolation).
- Document browser, labels, and scoped questions (2026-07-25, user request: "visualize
  everything the uploads connector has ingested… provide aka and tags… scope my question to a
  specific connector/document so not too many LLM calls are spent searching and there's less
  ambiguity"). Design decisions taken with the user first: a **deterministic picker** rather
  than natural-language scoping (resolving it with the model would cost the very round-trip
  scoping exists to save), folder tags **inherit automatically**, and a scoped turn also
  **drops live tools for excluded connectors**. Full design in the architecture bullet above.
  Suite: **676 passed** (+17). Verified end-to-end in Chrome against a real synced workspace:
  the browser groups 13 documents by folder, a folder tag shows as inherited on both its
  files, a document tag added through the UI appears in the picker, and the chosen scope
  reaches `POST /api/chat` as `{"tags": ["architecture"]}`. Retrieval filtering measured
  directly: "leave policy days" returns `leave.md` at 0.78 unscoped and is **absent** when
  scoped to `#architecture`, while an in-scope question keeps its full 0.79 score. (The
  chat-level assertion needs a live LLM; that scratch workspace had no Ollama model pulled,
  so the store-level numbers are the evidence for filtering.)
- Corrupt-package salvage (2026-07-24): the user's real deck turned out not to be a text
  problem at all — it failed to open, `Bad CRC-32` on one embedded PNG, taking every slide's
  text with it. Extraction now salvages text straight from the package XML when the library
  cannot open the file. Suite: **659 passed** (+3). Detail in the `ingest/extract.py` bullet.
- PowerPoint extraction rebuilt on three passes after a user challenge (2026-07-24): "why don't
  you research how to build a perfect powerpoint reader/extractor? I think there's lots of text in
  this file but the parser is missing everything." They were right, and the earlier group-recursion
  fix was necessary but not sufficient. Researching python-pptx's source found the hard-coded
  six-tag whitelist in `iter_shape_elms` that excludes `mc:AlternateContent`, making those shapes
  wholly invisible to the object model; reproduced, then fixed by merging a raw DrawingML sweep and
  a referenced-parts sweep with the structured pass. Design in the `ingest/extract.py` bullet. Also
  added **`qj extract <file>`**, a workspace-free diagnostic that prints exactly what the extractor
  reads (per slide) — so "parser gap or picture-only deck?" is answerable in one command instead of
  inferred from a chat reply. Suite: **656 passed** (+3).
- Unreadable attachments are named, not hidden — and `/ingest` is deterministic (2026-07-24,
  user-reported, third round on the same flow). Two remaining holes after the previous fix:
  (1) when a file extracted to NO text, `build_context_block` returned an empty string, so the
  agent was told nothing at all — not the filename, not the id, not that a file existed — and
  flailed, asking the user to "paste the attachment IDs" it had never been shown. Now every
  attachment reaches the model either as usable text or as a named UNREADABLE entry carrying
  its id and the recorded reason, with an explicit instruction not to ask for ids or re-uploads.
  The reason is persisted (`context_attachments.extract_error`, migration-added) at upload time
  by `store_attachment`, returned by the upload endpoint, and shown to the user in chat the
  moment it happens rather than being inferred from a confused answer. (2) `ingest <path>` kept
  failing agentically ("I can't read files directly from your computer") even after the tool
  description was corrected — so it now has a **deterministic** `/ingest <path>` slash command
  (`commands.ts` → `POST /api/uploads/local`, quotes stripped so a "Copy as path" paste works),
  matching the house rule that slash commands are the deterministic fast path and cannot be
  talked out of doing their job. Listed in the composer's command hint. Suite: **653 passed**
  (+2). Browser-verified: `/ingest "<path>"` → `uploads:uploads` 0→1 with a confirmation in
  chat, and an unreadable file reports why instead of appearing to succeed.
- Attachments can now be learned, and the agent knows it can read files (2026-07-24, user
  request + two live reports). Attaching a file in chat stays per-question context by default —
  the separation the user asked for originally — but "here's a document, learn it" is the obvious
  next thing to want, and there was no route to it: `POST /api/chat/attachments/{id}/learn` +
  a **Learning permanently** toggle in the composer + agent guidance now provide one. The
  companion bug: `ingest <local path>` was answered with "I don't have direct access to files on
  your computer", because `qj_api`'s tool description never mentioned document ingestion — the
  capability existed, the model just had no cue it did. Details in the `chat_attachments.py`
  bullet. Suite: **651 passed** (+4). Browser-verified end to end against a scratch workspace:
  staged chip → default "Ask only" → toggle → send → `uploads:uploads` 0→1 → the deck's
  grouped-diagram labels retrieved at 0.69–0.77, and the toggle resets after the send.
- PowerPoint/Word shape-tree extraction fix (2026-07-24, user-reported): an architecture deck
  attached in chat produced no context at all — the agent replied "I'll need the content". Root
  cause proven by reproduction, not guessed: `slide.shapes` walks only top-level shapes, so
  grouped diagram labels were lost silently. Fixed with group recursion + chart + SmartArt +
  Word text-box/header coverage; details in the `ingest/extract.py` bullet. This affected EVERY
  ingestion surface (chat attachments, Uploads, files/git, OneDrive learn), not just chat.
  Suite: **647 passed** (+3). NB the related UX point: attaching a file in chat is deliberately
  **per-question context, not memory** — permanently learning a document is `/qj ingest <path>`
  or `POST /api/uploads`.
- OneDrive / SharePoint connector — per user, on demand (2026-07-24, user request). Design
  decisions taken with the user before building: **communal memory accepted for now** (with the
  leak stated at connect time and per-user knowledge scopes raised to PRIORITIES #2), **both**
  sign-in flows (device code for CLI/Docker, auth-code+PKCE for the web UI), and all three access
  scopes. Then re-scoped mid-build on the user's instruction from a crawler to a **credentialed
  on-demand reader**: `/qj learn from this onedrive document <url>` ingests exactly what you
  point at, `sync` only refreshes what you already taught it. Full design in the connectors
  bullet above. Also in this change: **zip archives** and a wider text/code format set at the
  extraction choke point, images made an explicit *not-yet* wired to the vision seam, and a new
  `Connector.on_deleted()` hook. One core change was needed to support it — `sync(state)` may now
  write back into its state dict (`catalog.set_sync_state_many`, committed **before** `since` so
  a stale loaded value can't rewind the watermark, and only after a fully successful run), which
  is how a connector persists an opaque cursor of its own. Suite: **644 passed** (+46: 34
  `tests/test_onedrive.py`, 6 API, 6 extract), 12 skipped. Browser-verified via Playwright against
  a scratch workspace: the connector form renders all fields, the plate shows signed-out →
  Sign in with Microsoft → signed-in-as with the on-demand learn box, and a learn against a real
  Microsoft endpoint surfaces the genuine `invalid_grant` sign-in error to the user rather than
  failing silently. **Not verified: any real tenant** — no Microsoft 365 app registration or
  account is available on this machine, so the Graph request/response shapes are covered by
  MockTransport round trips against the documented contracts, not by a live drive.
- Knowledge-graph canvas rebuilt — smooth camera, live layout, map-tile LOD (2026-07-24, user
  report: "it's like nothing", with a screenshot of the live AppRiver graph rendering as
  one-pixel dust; and "when I zoom in, expand neighbours, then zoom out, everything becomes
  disproportionately small"; follow-up ask: "some sort of caching / layered data rendering like
  map tiles to manage too much data"). Root causes were all in the view, not the data: the SVG
  `viewBox` was snapped into React state per event (so interaction stepped), node radii were in
  **world** units (so any wide fit shrank them to nothing), and the layout was a one-shot
  snapshot (so merges teleported). Full design in the frontend architecture bullet above.
  Live-verified in Chrome via Playwright against the real workspace (24,168 docs / 634-entity
  graph): mark size holds **7.8–18.5px across the entire zoom range** (was sub-pixel), a fling
  drags 526px then coasts 44px/109px decelerating after release, LOD culls 634→112 drawn nodes
  when zoomed in and the toolbar says "112 in view", zoom-out floors at content scale (0.116 vs
  the old 0.05 void), double-click expand fits the new neighbourhood at k=1.25, and the console
  is clean. No backend change; no API change. Frontend typecheck + build green.

- Connector-row browsing, zip upload, and real ingest progress (2026-07-26, three user reports
  in one session). **(1) Connected-systems row permanently reopened the sync log instead of
  ever browsing documents again**, on any connector that had synced at least once: `Rail.tsx`'s
  `watchable` gate read `job?.live`, which means "the in-memory `SyncManager` still owns this
  job id" — and finished jobs are never evicted from that map, so it stayed true forever after
  a connector's first sync/cleanup. Fixed to require the job actually be in flight (`syncing`,
  the same running/paused/stopping/retrying check already computed) — matches the code's own
  stated intent ("a system that is syncing right now… clicking it re-opens the live log").
  **(2) The composer's file picker couldn't select a `.zip`**, or dozens of other now-supported
  extensions — `UPLOAD_ACCEPT` was a stale, partial hand-copy of `ingest/extract.py`'s real
  support (drag-and-drop bypassed it, which is how the user got one attached at all). Rebuilt
  from `DOC_EXTENSIONS ∪ ARCHIVE_EXTENSIONS ∪ TEXT_EXTENSIONS`. **(3) A several-MB attachment
  froze the whole server for seconds**, not just the uploading tab: `POST /api/chat/attachments`
  and `POST /api/uploads` are `async def` FastAPI routes that called text extraction/ingestion
  *synchronously* — CPU/IO work running directly on an `async def` handler blocks the entire
  event loop, so every other request/SSE stream on the server stalled for that long too. Fixed
  with `run_in_threadpool` (existing `def` routes like `upload_local`/`learn_attachment` were
  already safe — Starlette auto-threads plain `def` handlers, only `async def` needed it).
  **(4) Even threaded, "Learning permanently" a large file (e.g. a whole zip, concatenated) can
  take real minutes to embed on a CPU-only machine, and the chat gave no sign anything was
  happening** — reported as "it just stayed there… then after a couple of mins it posted".
  Real (not fake) progress needed the embed step itself instrumented: `KnowledgeStore.
  upsert_document_with_progress` (new, deliberately a SEPARATE method from `upsert_document` —
  the hot path every connector sync calls, left untouched) embeds in small batches and reports
  `(chunks done, chunks total)` between them; `pipeline.ingest`/`_ingest_one` gained an optional
  `progress_cb` (`None` for every ordinary sync, unaffected); `ingest_progress.py` is a tiny
  in-memory registry keyed by a token the FRONTEND mints (so it can start polling
  `GET /api/ingest-progress/{token}` in parallel with the `POST …/learn?progress_token=…` that
  owns it — an HTTP response can't be sent twice, so the token must exist before the slow work
  starts). `Msg.learning` (`Chat.tsx`) renders a real progress bar in the SAME agent bubble that
  goes on to stream the reply, and `App.tsx` now pushes the user message + that bubble
  **immediately** on send rather than after attach+learn finish, so nothing is silent while
  ingestion or embedding runs. Postgres/pgvector has no equivalent method — falls back to the
  plain call via `getattr`, same pattern as `ensure_ann_index`/`maybe_compact`.
  Also this session: the **document browser groups Confluence pages by space**
  (`DocumentsModal.tsx`) — a Confluence page uri is `{base}/spaces/{KEY}/pages/{id}/{title}`,
  and the page id makes every page's own path segment unique, so the generic folder-prefix
  grouping gave each page its own one-document "folder" (reported: "for all the pages under AR
  there's 1 individual entry with no parent as space"). Grouped instead by the prefix through
  the space key — still a real uri prefix, so it stays valid as the `uri_prefix` a folder label
  is stored against, no backend change needed. And **an ingested `.zip`'s contents are listed in
  the document browser** rather than showing as one opaque row: new `GET /api/sources/{id}/
  documents/archive?doc_id=` (`ingest.extract.parse_archive_manifest`, pure) re-derives the
  member list by re-reading the same `--- path ---` headers `_extract_zip` wrote into the
  document's own text — chunks are the only place that text lives, so no new schema column.
  Suite: **685 passed** (+9), 12 skipped, pre-existing eval-yaml failure unchanged; frontend
  typecheck + build green. NOT yet live-verified in the browser against a real multi-minute
  embed (would need a genuinely large corpus on this machine to observe end-to-end) — the
  progress math itself is unit-tested (`test_pipeline.py`, `test_chat_attachments.py`) and the
  wiring was traced by hand through every layer.

- ADO/TFS work-item hierarchy graph + document-browser tree view shipped (2026-07-26,
  AI_ROADMAP #26 / PRIORITIES #20 — closes the Jira/ADO relationship asymmetry the roadmap had
  flagged; user asked "have we implemented harvesting TFS relationships — epic → feature →
  story/bug → task/pipelines/development work/other related stories?", answer was no, then asked
  to build it plus a hierarchical explorer). Full design in the `azure_devops.py` architecture
  bullet above (`hierarchy_graph`/`_merge_graphs`, the bounded hierarchy walk-up, per-document
  display metadata + its version-triggered backfill, and the document browser's
  `buildWorkItemTree`/`sortSiblings`). Plan reviewed and approved with the user before
  implementation (`~/.claude/plans/smooth-humming-forest.md`); two design choices confirmed with
  them directly: "completed" is a state-name heuristic (not an extra ADO API call), and completed
  items sort by closed-date-falling-back-to-last-updated. Suite: **695 passed** (+10), 12 skipped,
  pre-existing eval-yaml failure unchanged; frontend typecheck + build green. Scoped to Azure
  DevOps only per the request — Jira already has `part_of` edges and could adopt the same tree
  view later without rework, but that's still just a note, not built. **Live-verification pending
  a real ADO re-sync**, same caveat as the ingest-progress entry above — the hierarchy walk-up and
  metadata backfill only exercise for real against a genuine sync.
- Hierarchy walk gained a DOWN direction (2026-07-26, same-day follow-up): the user re-synced and
  asked "why did the connector pull only partial data for features and epics?" with a screenshot
  of the real ADO hierarchy for comparison. Verified directly against the live catalog rather than
  guessing (queried `catalog.db` for the Epic/Feature in question) — confirmed a whole Feature
  ("Tech Debt") was missing entirely and a discovered one ("Provisioning") had only 3 of its ~10
  real children. Root cause: the walk only ever went UP from a leaf still inside a team's recent-
  sprint window, so a Feature/Epic whose children were mostly old/completed was either never
  discovered or only shown through whichever single descendant happened to still qualify. Fixed by
  also walking DOWN (Hierarchy-Forward) from any discovered Epic/Feature specifically — full detail
  in the `azure_devops.py` architecture bullet's "Walks the hierarchy both UP and DOWN" note. Suite:
  **696 passed** (+1), 12 skipped. Still pending: watching the NEXT re-sync actually pull the
  previously-missing items (this fix ships the code; it hasn't been observed live yet).
- Document-browser filters for TFS work items (2026-07-26, same-day follow-up — a proposed
  bundled "detailed per-team sprint config + filters" change was rejected first; this is the
  filter half only, re-requested on its own). `work_item_document`'s `display` metadata
  (`azure_devops.py`) gained `assigned_to` and `tags` (ADO's `System.Tags` is one
  semicolon-separated string — split into a real list so filtering matches an exact tag, not a
  substring of the raw field). The document browser's Epic/Feature/Story/Task tree
  (`DocumentsModal.tsx`) gained three filter dropdowns for `sourceType === "azure_devops"` —
  iteration, assignee, tag — populated only with values actually present (never hardcoded),
  ANDed with each other and with the existing free-text name search. Reuses the exact
  ancestor-preservation logic the text filter already had (a matched deep child keeps its
  ancestor chain visible, so filtering to one tag doesn't orphan a Story from its Feature/Epic).
  No new endpoint or schema change — everything needed already rides the `metadata` blob
  `/api/sources/{id}/documents` returns. Suite: **697 passed** (+1), 12 skipped; frontend
  typecheck + build green.
- Dormant-team visibility for a project synced with `teams` left broad (2026-07-26, same-day
  follow-up): the user noticed ADO's own Teams page lists plenty of teams with nothing shipped
  in a year or more, and asked how the recent-sprint window behaves for them — and separately
  confirmed the live tools' scope. Answered directly first: `ado_query_work_items` (WIQL) and
  `ado_get_work_item` run project-wide, unscoped to `teams`/ingestion state, so a question about
  a never-ingested (or skipped-as-dormant) team still gets a live, current answer — verified by
  reading both tool bodies, not assumed. And a genuinely dormant team's recent-N-sprints window
  was already correctly empty (nothing wrong was being ingested) — the real gap was silence:
  indistinguishable from a transient API hiccup. Fixed with a pure visibility change, no
  ingestion behavior change: `_team_is_dormant(latest_checked_iteration, stale_after_days, now)`
  in `azure_devops.py` — when a team's checked window has zero items AND its most-recently-
  checked sprint ended more than `stale_after_days` (new option, default 365) ago, `sync()`
  reports it plainly via `self._stage(...)` ("no activity in over Nd, skipped") instead of
  silently moving on; an empty-but-recent window (a team just between sprints) gets a milder
  "nothing in this window" note instead of the dormant label. Suite: **698 passed** (+1), 12
  skipped; no frontend change (backend-only, no new metadata surfaced to the UI).
- Jira brought toward parity with the ADO connector's hierarchy/relationship work (2026-07-26,
  same-day follow-up): the user asked for a gap analysis between the two "similar nature"
  backlog connectors, then asked to implement the cheap/high-value items identified. Shipped:
  `related_to` edges from Jira's `issuelinks` (previously not even fetched), a bounded walk-up
  for parent Epics the incremental JQL pull would otherwise never reach, the same `display`
  metadata block ADO's document-browser tree reads (id/parent_id as Jira KEYS — strings, not
  numbers), and a `jira_get_issue` live tool (full description/links/comments — `jira_search`
  only returns summary lines). The document-browser Epic/Story/Task tree in `DocumentsModal.tsx`
  was generalized from ADO-only to a small source-type set, with zero Jira-specific UI code —
  this incidentally fixed a pre-existing bug where every Jira issue rendered as its own one-item
  "folder" (same class of bug Confluence had before its space-grouping fix). Full detail in the
  new `jira.py` architecture bullet above (added in the same change — jira.py had no dedicated
  bullet before this). Deliberately NOT done: Jira sprint/board windowing (a genuinely different
  API surface — the Agile REST API, not the base one this connector speaks — flagged as an open
  question in the analysis, not an assumed gap) and a bonus finding unrelated to Jira
  (`azure_devops.py`'s own webhook handler drops `repo_names`/`team` on push-ingested items).
  Suite: **701 passed** (+4), 12 skipped; frontend typecheck + build green.
- Webhooks made actually usable for systems that can't sign, + git push-triggered re-sync
  (2026-07-26, same-day follow-up to the "what purpose do hooks play, are they pre-configured"
  / "what functionality do webhooks bring" questions). Investigating the pre-configured question
  surfaced a real code gap, not just a docs one: `octopus.py`'s own docstring already admitted
  "Octopus can't sign, so front it with a proxy... or use a secret URL path via the source name"
  — but `hooks.py` never actually implemented that fallback, so Octopus's push mode (and, almost
  certainly, Azure DevOps Service Hooks, which has no native per-request signing either) had no
  real way to authenticate at all. Fixed with a fourth verification scheme in `verify_signature`:
  `POST /hooks/{source}?token=<webhook_secret>` — a plain shared secret in the URL itself, for a
  sender that can only configure a bare callback URL with no custom headers. Also shipped: the
  git connector's push-triggered re-sync (see its architecture bullet above) — asked for in the
  same message. Both land through the same `receive_hook`, which now takes a `syncs`
  (`SyncManager`) argument so a connector's `wants_resync` can kick off a real background sync
  job instead of always assuming a direct handle_event→ingest. Suite: **705 passed** (+4), 12
  skipped. A step-by-step setup guide for TFS/GitLab/Octopus was published as a Claude Artifact
  rather than embedded here (living reference material, not architecture).
- Plan-05 close-out: IVF_PQ grounding fix + coupled retune + two graph defects (2026-07-29,
  working the top of `docs/PRIORITIES.md`). **(1) `retrieval.ann_refine_factor`** (new, default
  10, `ge=1`) — `KnowledgeStore._dense` now re-ranks ANN candidates against un-quantized vectors,
  fixing scores that LanceDB's default IVF_PQ index was distorting by 0.25–0.45 cosine on any
  workspace past `ann_min_rows`. The suite now builds a **real index** for the first time (no
  prior test ever did, which is why this shipped at all), and the guard is verified to fail
  without the fix. `ge=1` is deliberate: LanceDB *raises* on 0, so the obvious "turn it off"
  value would have broken every dense search — it's rejected at config validation (clean 400
  through `PATCH /api/settings`) rather than left to explode at query time. Postgres is
  unaffected (pgvector HNSW returns true distances; no product quantization).
  **(2) `retrieval.min_score` 0.55 → 0.64**, coupled to (1) since the old value was calibrated
  against the distorted distribution. Chosen conservatively **against** `--calibrate`'s own
  max-margin pick of 0.72: 0.64 costs no answerable case its grounding on the eval set while
  gating 3 of 12 refusal near-misses (up from 0); 0.72 would gate 6 but cost 3–4 answerable
  cases. **(3) Eval set 21 → 32 cases** (`docs/evals/multi-hop-crosssource.yaml`, 4 → 12 refusal
  cases, clearing `CALIBRATION_MIN_CASES`; +3 relation-shaped person/team cases so a future C4
  re-test can actually detect the org-chart layer). Candidates that turned out to have genuine
  adjacent content in the corpus were rejected rather than shipped as fake refusals.
  **(4) `catalog.upsert_entity` no longer lets an LLM-proposed name clobber a well-cased
  deterministic one** — resolved inside the `ON CONFLICT` CASE, not read-then-write, so the
  hottest graph-write path gains no round trip and no race between concurrent source syncs.
  **(5) `graph.triple_workers` 4 → 16** (measured p50 ~18 s/call: 4 meant ~10 h to drain a real
  corpus). Also fixed: `.gitignore`'s unanchored `evals/` rule had been silently swallowing
  `docs/evals/` since the initial commit, so **the eval set this plan is built around had never
  actually been committed**. Suite: **710 passed**, 13 skipped (the new Postgres parity test is
  env-gated). **Not verified: the Postgres path** — Docker was unavailable, so the shared
  `ON CONFLICT` statement is SQLite-verified and only statically reviewed for pg (a syntax error
  would surface on any insert, but the CASE semantics are untested there). Also unverified: any
  agent-layer effect of the new threshold — that needs ≥3 replicates per the plan's own
  methodology note and real LLM spend.
- Settings changes now reach the running process (2026-07-30, user-reported: "I've these 2 on
  but it seems the graph didn't get extracted when I did clean resync", with both **Extract
  relationships from prose (LLM)** and **Entity resolution** shown enabled). Not a usage mistake
  and not a graph bug — confirmed against their live container, which reported saved config
  `graph.extract_triples: true` alongside `/api/graph/pending` → `extraction_enabled: false`.
  `PATCH /api/settings` persisted the value and mutated `ctx.config`, but the pipeline holding
  the triple extractor / entity resolver is built once in `build_context` at process start, so
  the re-sync ran with the extractors the process was born with. Fixed with
  `AppContext.apply_config()` called after every settings write — full design in the
  `quickjoiner/api/` bullet above, including why query-side knobs read from `ctx.config` were
  already live (which is what disguised it) and why the store is mutated rather than reopened.
  Suite: **755 passed** (+1, verified to fail without the fix), 13 skipped. No API surface
  change, so Swagger/Postman/Bruno are untouched; no frontend change — the drawer's own
  "ingest-time — re-sync to apply" hint was already correct and only now actually true.
  **Honest limitation, stated rather than papered over:** documents ingested while the extractor
  was absent were never marked `graph_pending` (nothing deferred them), so `drain-graph` cannot
  recover them — only a clean re-sync will. Turning extraction on could reasonably enqueue
  already-ingested qualifying documents for the drain; that's a genuine follow-up, not built.
- Crawl identity + connector-guided knowledge extraction (2026-07-30, same session, two user
  reports on the same corpus). **(1)** "Is the scraper preventing duplicate pages, does it stay
  inside the site's boundary, and why are there numerous homepage entries?" — measured against the
  live crawl first, which split the question cleanly: the boundary and per-pass URL dedupe were
  **already correct** (200/200 documents on the start host, zero TFS links followed, no URL fetched
  twice), while raw-string URL comparison, byte-identical pages at different URLs, a static
  site-wide `<title>`, and silence about a truncated crawl were **real defects**. Fixed with
  `canonical_url`, a `same_host_only` hard floor, exact-content dedupe, `page_title`'s `<h1>`
  preference, and truncation/duplicate reporting — full design in the `browser/` bullet — plus the
  document browser now showing each document's **real URL with its query string** beside the title
  (`relativeUri`), which is what made 171 distinct `/Details?id=…&project=…` pages visible as
  distinct instead of 171 rows reading "Home page". **(2)** "I want it to capture as much important
  information as possible… maybe we need a prompt that can guide knowledge extraction as part of
  connector configuration" — shipped as the generic **`extraction_prompt`** connector option (see
  the `specs.py` bullet). Worth stating plainly: the *cross-page* understanding the user asked
  about is the knowledge graph itself, not a bigger context window — "team Acadia owns repo X" from
  one page and "repo X depends on Y" from another join because both resolve to the same entity, so
  the answer to "which team owns the services behind Z" is assembled from pages no single prompt
  ever saw together. The guidance improves what each page *contributes*; the graph does the
  joining. Suite: **765 passed** (+10), 13 skipped; frontend typecheck + build green. Not yet
  verified live: no re-sync has been run with a guided prompt against the real corpus, so the
  quality of what a hint actually buys is untested on real pages.
- Shipped config defaults now reach workspaces that already exist (2026-07-31 — the top of the
  PRIORITIES backlog, and the open question the previous session ended on). The settings
  blob is persisted sparsely (`exclude_defaults=True`) so an untouched field follows `config.py`
  instead of being frozen at whatever it was the first time that workspace saved, plus a
  logged, once-per-workspace reconciliation of the three defaults this repo has ever changed
  (`config.SUPERSEDED_DEFAULTS`, guarded by `DEFAULTS_EPOCH`). Full design in the `memory/`
  bullet above, including why a deliberately-set value equal to today's default now follows a
  future retune and why that trade is the right way round. Grounded in measurement at both
  ends: git history says exactly three defaults ever moved (so the migration map is complete,
  not guessed), and a copy of the real 11-connector workspace confirms the outcome —
  `min_score` 0.55→0.64 adopted and announced, the deliberate `triple_workers: 8` and the
  already-current reranker untouched, connectors intact, idempotent on re-load. The practical
  payoff: plan 05's retune finally applies to the only real corpus, so its measured benefit
  (3 of 12 refusal near-misses gated at zero answerable-recall cost) is realised rather than
  theoretical. Suite: **769 passed** (+4, both behavioural tests verified to fail with the fix
  reverted), 13 skipped. No API or frontend change — `/api/settings` dumps the in-memory
  `Config`, which is complete either way.
- `qj bench` — the latency/cost harness (2026-07-31, AI_ROADMAP **S1**, PRIORITIES top of
  backlog after the config fix above). Design in the `bench/harness.py` bullet. The point of
  the item was that speed and cost were **not measured anywhere in the repo**, so every
  speed decision was taste; it now has the same shape of gate quality has had for months.
  It earned its keep immediately, which is the part worth remembering: the first run on the
  real corpus showed a **3.08s p50 query** — a number nobody had ever seen — and attributed
  27% of it to alias expansion, whose own code comment asserted the opposite ("every lookup
  is an exact indexed hit so the extra pass is cheap"). `EXPLAIN QUERY PLAN` confirmed a
  full scan of 36,203 entities per window, ~30 windows per query, because
  `WHERE id = ? OR LOWER(name) = ?` cannot index the second branch without an expression
  index. One line in `_MIGRATION_STATEMENTS` later: `expand_query` **382ms → 1.88ms**, whole
  query **p50 −29%, p95 −52%**, verified with the harness's own `--compare`. **The lesson to
  carry:** every correctness test passed before and after — a performance defect of that size
  was invisible to the entire suite, and a confident comment stood in for a measurement for
  months. The remaining 83% is the cross-encoder (~77ms/candidate, linear in depth), which is
  a *quality* trade and therefore S4's decision to make with eval data, not a bug to fix here.
  Live agent numbers (gpt-oss-120b via the litellm broker): first token 22.3s, full answer
  32.7s, 22.4k tokens/answer over 3 rounds, **0 cache reads** — closing S5's pending
  cost-delta question with an uncomfortable answer rather than leaving it open. Suite:
  **783 passed** (+14), 13 skipped. Not verified: the Postgres path (no Docker on this
  machine) — the expression index and the pg `search(trace=)` instrumentation are
  statically-reviewed only; and sync-throughput benching, which is deliberately left as a
  named S1 remainder rather than faked with a synthetic sync.
- Tables become structure, and the graph gets an enumeration read (2026-07-31, user-reported:
  "it can pull team names with count but not the member names — asking per team works").
  Diagnosed by measurement against the live container and the local 8-source workspace, which
  split the question cleanly and produced a better design than the first two proposals (both
  offered, both rejected in favour of what the data showed). **The user's own question — could
  something like Firecrawl help? — was answered honestly as no:** it solves acquisition, which
  this repo already solved for an internal, corporate-CA, session-authenticated host that
  Firecrawl's cloud cannot reach, and does NOT solve querying (it returns JSON, not a queryable
  store). The *pattern* behind the question was right, so it was built in-repo where the crawl,
  the documents and the catalog already are. What the measurements found, in order of severity:
  (1) HTML tables were flattened, and a **blank cell vanished** so later values shifted column —
  a silent fidelity bug on every scraped table, unfixable by any amount of retrieval tuning
  since the information was already gone at ingest; (2) **0 of 17** team pages produced any
  edge, while the same pages' key-value blocks extracted fine — structure, not the model, was
  the difference; (3) **1 of 875** person entities was shared across sources (Plumber names
  people by email, Confluence by display name, TFS by full name — three disjoint node sets);
  (4) the top-8 chunk window for the aggregate question was consumed by one overview page plus
  seven chunks of a single 323-person roster that carries **no team column at all**, so the
  model answered correctly from bad input. Shipped: table-preserving extraction, `ingest/
  tables.py`, email→person aliasing, `catalog.graph_relations` + the `graph_relations` agent
  tool + prompt guidance, and the `graph_neighbors` hub direction-grouping fix — all detailed
  in the bullets above. `GRAPH_EXTRACTOR_VERSION` 3→**4**. Suite: **801 passed** (+19), 13
  skipped. Also confirmed by measurement and worth keeping: **cross-source joining already
  works** — every source pair meets on shared entities (TFS↔Confluence 459, Plumber↔TFS 146,
  `package:appriver.core.logging` known to all 7 sources), and **45% of the entities a newly
  ingested web page touches already exist** in the graph and join automatically via entity-key
  parity plus the 1,472 `same_as` bridges. No API or frontend change (Swagger/Postman/Bruno
  untouched). **Not yet verified live: the new edges on the real corpus** — the table extractor
  only fires on ingest, so it needs a clean Plumber re-sync, which will also shed the **363
  ASP.NET error pages** (half that corpus) that predate the crawl-identity dedupe work; and
  **395 `graph_pending` rows** are waiting on `qj drain-graph`.
- Three backlog items closed, and the Postgres gate finally opened (2026-08-04, working the
  top of `docs/PRIORITIES.md`). **(1) AI #31 — error pages are no longer ingested as content**
  (`looks_like_error_page`): an ASP.NET app renders its error page with a 200, so 363 of 728
  documents in the live crawl were the same failure message, answerable and citable. Detection
  is deliberately stricter than its `looks_like_login` sibling because this one *skips* rather
  than reports. **(2) S1's remainder — ingest throughput** (`run_sync_bench` + `--sync-days`),
  read from the runs already in `sync_events` rather than by performing one; `SyncManager` now
  records `ingested` on stopped/errored runs too, since a long interrupted crawl is exactly the
  run whose throughput you want. **(3) FE F0's density budget, server half** — the whole-graph
  view was spending its budget on edges whose other end it never drew. Measured before changing
  anything and again after, on the real 109k-edge graph: **656 nodes / 392 edges / 91%
  degree-1 → 174 / 309 / 40%**, all 14 entity types kept, and 2.3x faster (2058ms → 883ms);
  `totals` + `truncated` now ship with the sample so the toolbar can say "· sample of 109,003".
  **The part worth remembering** is what running the env-gated Postgres suite found the moment
  Docker was available — it had not been run since 2026-07-10, and two defects had accumulated
  behind the gate. One was a genuine production bug in code that had passed static review: a
  `-- 57% degree-1` **SQL comment** broke every whole-graph query on Postgres, because psycopg
  reads a bare `%` anywhere in a statement as a placeholder. The other was a stale test still
  asserting pre-2026-07-21 prune semantics. Both are now guarded — the `%` escape moved into
  `_pg` itself (so it covers the inline SQL that cannot be enumerated) with a **pure** test that
  runs without Docker. Rule of thumb this earns: *"statically reviewed only" on a second backend
  means untested, and env-gated suites rot silently — run them whenever the gate can be opened.*
  Suite: **813 passed**, 14 skipped (all 14 pg tests pass when pointed at a real pgvector).
- Knowledge scopes — the enforcement core (2026-08-04, **PRIORITIES #1**, the item gating
  multi-user GA). Design and the two deliberate remainders are in the `auth.py` bullet above.
  Worth keeping from building it: **the retrieval half was already done** (2026-07-25's
  `SearchScope` filters both hybrid legs on both backends), so the work was almost entirely
  the *other* channels — the graph, entity autocomplete, corroboration counts, sampled
  totals, the gaps backlog. Filtering search alone would have produced a feature that looks
  finished and leaks: an entity NAME mined from a private document is disclosure on its own,
  and `graph_relations` — the enumeration read — would have been the single most efficient
  way to dump a private source's relationships.
  Two design points earned their keep immediately. **Picking and being permitted are
  different kinds of narrowing**, so they AND rather than share one list; the escalation
  test (name the private source in the picker) is the one that proves it. And the empty
  allow-list renders as an explicit false predicate because the obvious spelling, `IN ()`,
  is invalid SQL — the failure mode would have been failing *open*, silently, for exactly
  the user who may read nothing.
  Verified rather than asserted: the end-to-end two-user API test was **run with the filter
  disabled and confirmed to fail**, and the Postgres parity test was **executed against a
  real pgvector container**, not statically reviewed — per the rule the previous session
  earned the hard way. Suite: **841 passed** (+28), 14 skipped; 15/15 pg tests green;
  frontend typecheck + build green.

- Team rosters were never actually readable — two silent table defects (2026-08-05,
  user-reported: "list all teams and their team members… the confluence charter for
  caffeine still didn't return all team members, just Etheria"). Diagnosed against the
  live workspace rather than guessed, which split one complaint into two unrelated bugs
  in two different files, each individually sufficient to lose a whole roster.
  **(1) Confluence never rendered its tables** — the 2026-07-31 table-fidelity work
  landed in `ingest/extract.py` and `browser/scraper.py` but not in `confluence.py`,
  which kept flattening with `get_text()`. The stored chunk for the Caffeine charter
  read `Product Owner / Delivery Manager / Etheria Hill / Team Lead / Liu Maumasi …` —
  the empty Product Owner name is dropped, so Etheria sits directly under two role
  labels and every later pairing is off by one. Answering "just Etheria" was the model
  reading that faithfully; the information had already been destroyed at ingest.
  **(2) The Plumber roster's header was demoted to a data row** — it ends in an empty
  spacer column, and header detection required every cell populated, so the table
  rendered headerless and `ingest/tables.py` typed no columns: **0 edges from 7
  members**, and `person:*` → `team:caffeine` was empty in the live graph (verified by
  query, not inferred). Both fixed, both regression-tested, and **both tests confirmed
  to fail with their fix reverted** — the header one needed the both-ways guard too,
  since a tolerance that promotes any sparse row would delete real data from the body.
  Suite: **844 passed** (+3), 15 skipped. **Needs a re-sync of Confluence and Plumber**
  to take effect — the text changes, so the hash changes and both re-ingest normally; no
  `GRAPH_EXTRACTOR_VERSION` bump (that is for extractor changes the document text does
  *not* reflect, and would force needless rework across 21k documents).
  **The second half of the report — "there are 3 ways to answer this and I got one" — was
  a design gap, not a regression, and was built in the same session** (user chose it over
  a prompt-only nudge): see the `agent/divergence.py` bullet above. Established first by
  reading the code rather than the docs: the `candidates` carousel is wired end to end
  (parser → SSE → `CandidateCarousel`) and was never dead, but `prompts.py` gated emission
  on *"Only when the user explicitly asks for multiple interpretations/options/angles…
  Otherwise never emit that block"*, no tool compared source provenance, and only
  `graph_path` wrote to the confidence ledger — so even a well-formed search-derived
  candidate would have rendered "unscored". The counter-intuitive half is the graph one:
  `prompts.py` correctly routes "list all X with their Y" to `graph_relations`, whose
  answer is a **union** of every system's assertions — so `Caffeine (3)` was printed while
  *neither* system actually claimed all three people.
  ⚠ **The first implementation of this was wrong in two ways and was corrected in the same
  session, only because it was measured against the real corpus afterwards** — the
  detail is in the architecture bullet, and the lesson generalises: a plausible heuristic
  over provenance fired on **80% of ordinary questions** (no threshold separated the
  classes — it was deleted, not tuned), and comparing raw entity names reported naming
  variants as conflicts. Every test passed both before and after that correction, exactly
  as with the alias-expansion index bug: **a suite proves a feature does what it says, never
  that what it says is worth saying.** New behaviour that fires on a judgement call needs a
  base-rate measurement on real data before it ships, not just green tests. The same pass
  also caught that the **env-gated Postgres suite had been skipped** for a change to shared
  `_EDGE_SELECT` SQL — Docker was available and the house rule says run it; 15/15 pass and
  `evidence_source_id` was verified to actually populate there, not merely to select.

- Graph rebuild without re-fetching or re-embedding (2026-08-06, user request: "since our
  chunking mechanism hardly changes we don't need to retrieve things again — rather we just
  need to rebuild the graph… available everywhere: CLI, API and UI"). Shipped as
  `qj regraph [name] [--with-triples]`, `POST /api/graph/rebuild` (+
  `GET /api/graph/rebuild/preview`), a **Rebuild graph** action on every connector plate, and
  a workspace-wide one in Settings → Knowledge graph; job kind `regraph`, sentinel source
  `"knowledge graph"`, mutually exclusive with every other job in both directions. Design and
  the two reported paths are in the `ingest/pipeline.py` bullet.
  **The measurement that changed the design, taken before writing any of it:** the obvious
  implementation — walk the documents, re-derive edges from stored text, replace — would have
  silently destroyed **27% of the live 100,341-edge graph**, because a connector's structural
  claims are computed while FETCHING and **zero** documents persisted them (`metadata_json`
  held only the `display` block). That is the whole reason `documents.graph_json` and the
  faithful-vs-preserved split exist. Presented to the user with the three options and the
  measured cost of each; they chose never-destructive, so a document with no stored payload
  has its existing edges carried across and the rebuild can add and correct there but not
  remove — stated in the preview, the job log, the CLI and the UI rather than left to be
  discovered. `GRAPH_EXTRACTOR_VERSION` 4→5 carries the payload onto the existing corpus on
  the next ordinary sync with no re-embed, after which rebuilds become fully authoritative.
  Suite: **875 passed** (+17), 15 skipped; the preserve guarantee verified to fail with
  preservation disabled; all new catalog methods and the new column **run against real
  pgvector**, per the rule the earlier session in this file learned the hard way.
- Double truncation on `graph_relations`/`graph_neighbors`/`graph_path` fixed (2026-08-07,
  user-reported: "I only presented a subset of the teams found... graph_relations returned 334
  relationships and was truncated"). Traced to the agent loop, not the graph tools: `agent.py`'s
  `_cap()` applies the flat `chat.live_tool_result_max_chars` (24000) char cap to EVERY tool
  result, with no awareness that these three tools already implement their own bounded,
  self-describing truncation contract (`graph_relations`: capped at 400 rows, "truncated at N —
  there are more" only when it actually is). The 334-row case was under that 400 cap — the tool's
  own truncation flag correctly never fired — but the raw rendered text still exceeded 24000
  chars, so the outer cap sliced it anyway, replacing the honest "complete" state with a generic
  `[tool output truncated]` marker and dropping teams past the cutoff with no signal the model
  could act on. Fixed by exempting these three tools from the outer cap
  (`AppContext._UNCAPPED_TOOLS`, `OnboardingAgent(uncapped_tools=...)`) — full design in the
  `agent/` architecture bullet above, including why `search_memory` is deliberately NOT exempted
  (its `top_k` is model-controllable, so it has no comparable hard bound). Suite: **878 passed**
  (+2), 15 skipped. Not yet verified against the live 334-relationship case that triggered the
  report — the fix is unit-tested (cap skip + the unchanged `graph_relations` 400-row contract),
  not yet observed end-to-end against the real corpus.
- Whole-conversation markdown export (2026-08-07, user request: "I want the ability to
  download the entire conversation including thinking trace", plus a follow-up asking for
  the trace to be collapsible in the md as well). Shipped as a **Download conversation**
  button in the chat pane — design in the `Chat` frontend bullet above. Worth recording is
  what scoping the feature turned up: **thinking traces are never persisted anywhere
  server-side.** They arrive as SSE `thinking` events and live only in the browser tab's
  `Msg.thinking` state; `get_session` returns only `role`/`content`. So the export is
  deliberately client-side and states its own limit rather than implying a fidelity it
  cannot deliver — a reloaded page exports Q&A with no traces. The collapsible ask is met
  with GFM `<details>`/`<summary>`, which mirrors the in-app disclosure and renders natively
  in GitHub/VS Code/Obsidian (a plain-text viewer shows the literal tags — inherent to
  markdown, not worked around). The sources list re-runs the app's own `CiteBook` pass
  rather than reimplementing citation numbering, so exported `[n]` and inline `[n]` cannot
  disagree. Frontend-only: no API, schema, or Python change, so Swagger/Postman/Bruno are
  untouched. Verified by typecheck + build + a real browser export (no frontend unit-test
  suite exists — manual/Playwright verification is this repo's established practice).
  Spec: `docs/superpowers/specs/2026-08-07-download-conversation-design.md`.
- Reasoning trace rebuilt as a step-by-step timeline (2026-08-08, user request: "when I ask a
  question to Confluence ROVO I see this kind of thinking visualization instead of our flat
  text based — can we implement something like this?", with a saved page and two screenshots
  of ROVO's, then "each step is expandable that shows details" and "incorporate this in the
  download conversation flow as well"). Design in the `agent/` and frontend architecture
  bullets above. **The gap was mostly in what the backend never sent, not in the CSS:** reading
  ROVO's saved page showed its trace is a timeline of reasoning interleaved with actions, each
  naming its argument ("Reading URL: https://…/Caffeine+-+Team+Charter") — while our
  `tool_call` event carried the bare tool NAME and `call.input` was dropped on the floor, with
  no event at all for the outcome. So no amount of frontend work could have produced this: the
  arguments and results had to start being sent (`tool_call` → `{id, name, args}`, new
  `tool_result` → `{id, name, ok, summary, chars}`), and the ordering between reasoning and
  actions had to be preserved rather than split across two UI regions.
  Worth keeping: the reason our steps get ROVO-like titles at all is that reasoning models
  emit their own `**bold**` section markers, and `splitThought` uses THOSE — the alternative
  (summarizing each step with a model) would have been a second inference per step and would
  have put invented words in a panel whose entire job is to show what actually happened. A
  blob with no markers stays one honest "Reasoning" step. Verified on the real 22,139-doc
  workspace with gemma4:cloud, not only against a scripted provider: the live panel showed the
  model's genuine reasoning about the grounding rules followed by its real
  `graph_relations(works_on, person→team)` call, and the exported markdown carried the same
  numbered steps with each tool's arguments and its result stated as "first 800 of 14,485
  characters". The scripted run additionally covers what a real run wouldn't reliably produce
  — a failing tool (rendered gold with a warning, `ok: false`) and an unknown tool name (still
  readable via the derived label). Suite: **879 passed** (+3), 15 skipped. Frontend rebuilt.
- Citations became links (2026-08-08, same session, user request: "in ROVO sources appear as
  clickable links to the actual pages/urls — can we implement citation that way so the citation
  chips are individually clickable and then compiled sources also give links?"). Design in the
  `agent/refs.py` and `CiteBook` bullets above. **The information was never missing, only
  undelivered:** every retrieval hit already carried its uri to the model, so this is a new SSE
  `sources` event and a resolver, not new retrieval. Two decisions are the substance of it, both
  following the repo's existing rule that a wrong citation is worse than an absent one —
  normalized-equality matching only (no fuzzy title matching), and the all-or-nothing split for a
  bracket naming several sources. Both were vindicated live on the real corpus rather than in
  theory: one run cited two *genuinely different* Confluence pages with near-identical titles
  ("Caffeine Team Charter" p5149196848 vs "Caffeine - Team Charter" p5108498435) and each chip
  linked to its own page; another cited a page plus its Confluence **space**, which is not a
  document we hold, and stayed honestly unlinked instead of half-linked. Measured on your
  workspace: 4 of 5 chips linked in one run, 3 of 4 in another, the unlinked ones being exactly
  the refs that name no retrieved document. Also fixed while building it: the sources excerpt led
  with the contextual-chunking breadcrumb, so it repeated the title and URL shown beside it
  instead of the document's first words. Suite: **893 passed** (+14), 15 skipped.
  **Follow-up the same session, and the lesson worth keeping:** the user reported citations
  still not linking, and they were right — I had shipped this having verified it only on
  `search_memory`-grounded answers, and closed out by NAMING the graph-evidence gap as a
  follow-up rather than measuring how much of real traffic it covered. It was most of it:
  the org-structure questions this product exists to answer route to `graph_relations`, so
  the feature was half-working for its most important case. The uri was already on the row
  and was being thrown away by an `or` chain — a one-line-shaped fix I had reasoned past
  instead of checking. **A known gap stated in a doc is not the same as a measured one**;
  had I probed the raw SSE for one graph-routed question — which took two minutes once I
  actually did it — the split would have been obvious before shipping.
  Their other suspicion ("is this because the answer is cached?") was also half right in a
  way worth recording: their tab was running the pre-fix JS bundle, so ZERO citations
  linked in that conversation while other conversations (loaded after the rebuild, and
  search-grounded) linked fine. **A frontend change needs a hard reload before the report
  it produces can be trusted** — worth asking about first, since it cleanly separates "not
  deployed" from "not working".
  **Still not linkable, deliberately:** a cited title that this turn's tools did NOT return
  (the model carrying it from a distilled `conversation://` doc, as observed live for
  "Black Team - Charter" / "Warehouse - Team Charter" — both real documents with real
  urls). Resolving those against the catalog by title would link them, and is the wrong
  call: a citation claims provenance, so linking one the turn never retrieved lends it
  credibility it has not earned — the same rule `candidates.filter_resolvable` already
  enforces. A `conversation://` uri is likewise never linked (it opens nothing).
  Final measurement on the reported question, in the browser: **8 of 10 chips linked**,
  then 7 of 8 on a re-ask — the unlinked ones being exactly the charters that turn's tools
  did not return.
  **Extended the same day to live tools and code files** (user: "tfs work items/pipelines/
  boards should appear as links, gitlab apis should also result in pure citable links, and
  for git repos I need links to actual code files based on the base path of the git repo").
  Design in the `weblinks.py` / `live_tools.cite` bullets above. Investigating it turned up
  a regression the citation work had itself introduced: a cloned repo file's uri begins
  with its clone url, so `href()` gating on "looks like a URL" was rendering **1200
  documents' citations as confidently broken links** — gating on a server-computed `link`
  is what fixes it, and is why identity and address are now separate fields.
  **Then Octopus and web pages** (user: "now let's fix octopus related sources citation,
  web pages citations as well") — where auditing first showed both were *already* linking
  (995/995 and 565/565), so the work that mattered was the Octopus live tool plus the two
  resolution rules the audit exposed: ambiguous titles and head-of-title citations, both
  detailed above. Suite: **914 passed** (+18), 15 skipped.
- Agent Skills — packaged expertise, per-user credentials (2026-08-09, user request; the
  session that started it disconnected mid-build and this completed it). Design in the
  `quickjoiner/skills/` bullet above. Connectors teach the system what the org knows; a
  skill teaches it **how the org works**, which nothing before this covered.
  **Format compatibility was treated as a constraint rather than a feature**: the two
  QuickJoiner-specific ideas (what a skill requires, who supplies it) are optional
  frontmatter keys the other runtimes ignore, so a skill stays portable in both directions.
  That decision paid for itself immediately — pointing discovery at the real
  `~/.copilot/skills` folder on this machine picked up **8 existing skills unmodified**, and
  they became the test corpus for everything else.
  **The security shape is the substance**, and each rule below is enforced in code and
  pinned by a test written as a leak test: a user-scoped skill resolves its environment with
  `include_process_env=False`, so it can never borrow the server's credentials and silently
  act as somebody else — it refuses and names the missing value; a model-supplied path
  escapes neither `read_skill_file`, `run_skill_script` nor the zip installer; a stored value
  is never readable back out by any screen, endpoint, CLI command or log line; and installing
  is admin-tier while *using* a skill and supplying your own credentials are viewer-tier,
  because a script runs with the server's privileges and this is explicitly not a sandbox.
  **What measuring against a real library changed.** Environment detection first matched
  `${NAME}` everywhere and turned one skill into 90 lines of local JavaScript variables, so
  the pattern is now scoped per language; `LOCALAPPDATA`/`ProgramFiles` were the most common
  reads across the whole set, hence `_SYSTEM_ENV`. It still cannot distinguish a credential
  from a tuning flag — the real `tfs-control` skill proposes 9 values of which 4 have
  defaults — which is precisely why detection is presented as a **suggestion** and the admin
  PATCH is the gate. One description alone ran to ~1,000 characters, which is what set
  `MAX_DESCRIPTION_CHARS`: only names and descriptions ride every prompt, so seven such
  skills would have cost more prompt than most answers.
  Verified live end-to-end against ollama `gemma4:cloud`, not only with a scripted provider:
  asked "how should I search our application logs?", the model chose
  `open_skill('query-logs')` unprompted, read the body and answered from it; asked to use a
  skill whose credentials were absent, it called `my_skill_secrets` and replied naming
  **exactly the one value genuinely missing** (the other two having been inherited from the
  workspace layer — the layering working as designed). Browser-verified in Chromium against
  a scratch workspace: Settings → Skills listed 9 skills at "3 of 9 ready for you", the
  scoped plate showed its two credential boxes, saving both flipped the badge to READY and
  the summary to "4 of 9", with no console errors.
  Suite: **952 passed** (+38), 15 skipped. `cryptography` added to `pyproject.toml` — it was
  imported by `secrets.py` and only incidentally installed here, so a fresh install would
  have failed to store a credential at all.
  ⚠ **Not verified: `run_skill_script` against a real credentialed system.** The runner is
  covered by tests (env layering, containment, refusal) and the real skills on this machine
  are PowerShell/Node against TFS/Octopus/Mongo endpoints this environment cannot reach, so
  no script has yet been run end-to-end with live credentials through the agent.
- Knowledge scopes completed — the merge guard and the promotion flow (2026-08-10,
  **PRIORITIES #2**, Cloud Y1.8 (c)+(f); the item that gated multi-user GA). Design in the
  `auth.py` and `promotion.py` bullets above. The enforcement core shipped 2026-08-04 and
  these were the two remainders it deliberately left, both real: a private document could
  still shape the org's canonical graph, and there was no way for anything private to ever
  become the org's.
  **What building it changed about the plan.** The merge guard was scoped as "don't let the
  resolver merge", and reading the write path found **three** doors to the same act, not
  one — the resolver's alias, `upsert_entity`'s rename rule (a materially different name
  silently replaces an org entity's display name everywhere), and the extractor-supplied
  alias rows a table or an `aka` produces. Guarding only the first would have shipped a
  feature that looks finished and leaks. The fourth was `same_as` bridges, which are the
  only edges carrying **no evidence document** and are therefore, by the enforcement core's
  own deliberate rule, visible to everybody — so a private-only entity bridged to an org
  one publishes its name no matter how well its documents are filtered.
  Scoping promotion turned up the reason the roadmap said "a metadata flip, not a copy" and
  what makes it possible: a `doc_id` is *derived* from `(source_id, uri)` at ingest and
  opaque everywhere after, so a document can change source and keep every citation, edge and
  label. Moving it to an **ownerless** bucket then means every visibility predicate already
  written starts including it — zero read-path change, which is why this is ~200 lines rather
  than a second visibility dimension threaded through a dozen graph reads.
  Two gaps found by USING it rather than by reading it, both fixed here: `GET /api/sources`
  was still listing catalog-only rows unconditionally (so `Notes from ada` and its document
  count were enumerable by everyone — the last enumeration surface the 2026-08-04 pass
  missed, on a comment that had been true when written); and the Rail lists only *configured*
  connectors, so a private notes bucket had no row at all and the author-side affordance was
  unreachable in the product even though the endpoint worked.
  Suite: **1047 passed** (+17), 16 skipped (+1: the new Postgres test, correctly env-gated).
  The merge-guard leak tests were **run with the
  guard disabled and confirmed to fail** (4 of them; the three controls — a shared source
  still merges, attaching still works, open mode unchanged — correctly pass either way, which
  is what proves the guard bites on privacy alone). **16/16 Postgres tests run against a real
  pgvector container**, including a new one covering the insert-only upsert, the LEFT-JOINed
  bridge query, `rehome_document_entities` and the in-place source move across both the
  catalog and the `chunks` table. Browser-verified end to end against a live two-user server:
  Bob's **Your private notes** row → **offer to org** → the chip flips to "offered", Ada's
  Settings shows the queue, expanding it shows the note's real text, **Publish to everyone**
  empties the queue, and Ada's search goes from `[]` to a 0.791 hit on the same document.
- Reranker right-sizing — the depth half of AI_ROADMAP **S4** (2026-08-10, top of
  `docs/PRIORITIES.md`). `retrieval.rerank_candidates` 24 → 16; design and the full curve are
  in the `memory/` retrieval bullet above. Live result: query p50 **1977 → 1401 ms (−29%)**,
  p95 −21%, recall/grounded-recall/MRR/hop-coverage **unchanged**.
  **What made this cheap enough to do properly** is worth keeping: cross-encoder scores are
  independent per candidate, so the entire depth curve comes out of ONE scoring pass over each
  query's fused pool — an eleven-depth × six-truncation grid that would have been ~40 minutes
  of real searches was 5 minutes of simulation. The safeguard that makes a simulation
  admissible is the first thing the script does: rebuild the pool, apply the real reranker at
  the configured depth, and check the result against `store.search` — **32/32 queries
  reproduced exactly**, and the subsequent live `qj eval` matched the simulated numbers to
  three decimals.
  **Two things measurement overturned.** The roadmap framed S4 as "cut the depth to save
  time", implying a recall-for-latency trade; there was no trade to make — quality is flat
  from 12 to 32, so the shipped 24 was simply doing twice the necessary work, and the honest
  cost of the change is zero. And the roadmap's other two candidate levers were checked before
  being believed: ONNX thread count is already optimal at its default (explicitly setting it
  made things *worse* — 1684 ms vs 2100 ms at 4 threads, 5350 ms at 1), and batching is
  already one forward pass for the whole depth. The genuinely new finding was that
  per-candidate cost is linear in candidate **length**, not just count — and that 18% of live
  chunks already blow past the model's 512-token cap and are silently truncated — which is a
  real second lever, deliberately left unshipped because its measured sign flipped with depth
  on a 20-answerable-case set. It is now AI_ROADMAP **S4a**, gated on a bigger eval set.
  ⚠ **`qj eval --compare` exits 1 on this change** and that is reported, not suppressed:
  `refusal_accuracy` moves 0.25 → 0.167 on one case that is correct at exactly depth 24 and
  wrong at 8, 10, 12, 16, 20 and 32. Non-monotone in depth ⇒ not caused by depth; the
  mechanism is that the gate reads dense cosine while reranking decides which chunks occupy
  the returned window, so a high-cosine chunk can fall outside it by luck. Holding depth 24 to
  preserve that number would be tuning to an artifact — the same trap the divergence work hit
  in 2026-08-05, and the reason a base-rate check comes before believing a metric.
  Tests: `tests/test_retrieval.py` — one pinning the **mechanism** (a candidate buried below
  the depth is never scored and so can never be promoted, making this a recall knob before a
  cost dial) and one guarding the measured floor of 12 against a future blind cut. Suite:
  **1049 passed** (+2), 16 skipped.
  Not verified: any agent-layer effect — that needs `--agent` runs with real LLM spend, and
  the agent numbers in the S-track baseline are unchanged since 2026-07-31.
- Architectural-layer classification — AI_ROADMAP **#29**, the classifier half (2026-08-10,
  `docs/PRIORITIES.md` #4). Design in the `ingest/layers.py` note above.
  **The measurement changed the feature, which is the part worth carrying forward.** Scoring
  the roadmap's own proposed signals against 15,469 real defining paths BEFORE writing any
  rules showed its first-named signal (layer-named directories) tags **3.3%**, and its full
  six-layer taxonomy **10.4%** of production files — because this corpus, like most
  enterprise .NET, is organised by domain (customeraccounts, sales, pricing, quotes) rather
  than by layer. Shipping it as specified would have produced an attribute too sparse to do
  any of the three jobs it was ranked for. The corpus instead named its own dominant
  categories — test scaffolding **33.7%**, vendored code **16.0%** — and adding those takes
  coverage to **59.9%** of defining paths / 54.8% of symbol entities. Had the vocabulary been
  written from taste, it would have been wrong in a way no test would have shown.
  ⚠ **Two defects survived a green unit suite and died on the real corpus**, which is the
  lesson: `_segments` lowercased before `_tail_word` read CamelCase, so multi-word names
  (`SubscriptionInfoDenormalizerTests`) yielded nothing and test detection silently fell to
  **0.5%** — every fixture name in the tests was short enough to survive it; and .NET names
  test PROJECTS not folders, which exact segment matching missed. A third was caught by a
  test I had written badly rather than by the code: the "ambiguous words stay untagged" case
  wrapped its fixtures in `src/Domain/`, which legitimately IS a layer directory, so the
  classifier was right and the test was wrong.
  Also corrected in flight: my own vendor probe matched "vendor" as a substring and claimed
  `GetOrderByVendorCodeResponseServiceModel.cs` — an org file about a *vendor code* business
  concept — as third-party, which is why `is_vendored` matches whole path segments only.
  Suite: **1062 passed** (+13), 16 skipped; frontend typechecked and rebuilt.
  Not verified: the layer on the live graph — `entities.layer` is written at ingest, so the
  real corpus shows it only after a sync re-provides its code documents (the
  `GRAPH_EXTRACTOR_VERSION` 5→6 bump makes that an ordinary sync, not a clean re-sync). The
  coverage figures above are from running the shipped classifier over the paths already
  stored on those edges, not from a populated column. Payoffs (b) the guided-tour spine and
  (c) layer as a retrieval signal are untouched and tracked as AI_ROADMAP #29a.

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
   guidance; `GET /api/graph?entity=&limit=` snapshot — which since 2026-08-04 also carries
   `totals` + `truncated`, so a caller can state what the sample left out; tests/test_graph.py).
   **`graph_snapshot` whole-graph sampling is TYPE-BALANCED (2026-07-23):** the view can only draw
   ~`limit` (400) of tens of thousands of edges, so it samples — and the sample must be
   representative. A per-src cap alone still front-loads whichever entity type sorts first and is
   numerous: after the GitLab sync the hundreds of `branch:` entities (sorting before every other
   type) consumed the entire 400-edge budget via belongs_to/for_ticket, so the whole graph rendered
   as branches+tickets and hid the services/deps/deploys/code that were fully present (the "graph
   looks like a subset" report). Fixed by giving each `src_type` an even share and round-robining
   across src within a type — a representative multi-type view (services/repos/deps/deploys/
   branches/MRs together) at any limit.
   **Extended to a CONNECTED CORE + stated totals (2026-08-04, FE F0's server half):** balancing
   fixed *which types* appear but not *whether they connect* — edges were picked without regard to
   whether their other end was also drawn, so the budget filled with dangling leaves and the view
   read as "nothing is connected" on a graph that is in fact dense. Measured on the live
   109k-edge graph: 656 nodes for 392 edges, **91% of them degree-1** — a field of stubs. Now the
   best-connected entities are chosen first, still evenly across types, and only edges with **both
   ends inside that core** are returned: **174 nodes, 309 edges, 40% degree-1**, with all 14 entity
   types still represented. The budget is a ceiling, not a target — 309 edges that connect read
   better than 392 that mostly don't — and it is **2.3x faster besides (2058ms → 883ms)**, since
   the expensive join now runs against a small id list rather than the whole edge table. The degree ranking is resolved as its **own query** rather than a CTE:
   SQLite re-evaluates a CTE joined twice (measured 900ms+ on the 109k-edge graph against 92ms to
   compute the degrees alone), so two indexed steps with the ids passed in is both faster and
   portable — no engine-specific `MATERIALIZED` hint. `graph_totals()` + a `truncated` flag ship
   **with** the sample (`/api/graph` passes them straight through; the GraphView toolbar renders
   "· sample of 109,003") — the same no-silent-caps rule the crawler and graph tools follow, since
   400 of 109,003 edges returned bare reads as the whole organization. Portable window-function SQL
   (both backends, and Postgres-verified — see the `%`-escaping note under `pg_catalog` below);
   regression-tested (`test_graph_snapshot_balances_across_types_not_one_numerous_type`,
   `…_samples_a_connected_core_not_a_field_of_stubs`, `…_states_what_it_left_out`, plus
   `test_pg_whole_graph_snapshot_and_relation_enumeration`).
   ~~Phase B~~ DONE 2026-07-11: `graph_path` (undirected BFS, evidence per hop — catalog +
   agent tool + `GET /api/graph/path`); Jira issue docs assert ticket→part_of→project/epic,
   Octopus asserts service entities + service→deploys→environment; React "Knowledge" view
   (`frontend/src/components/GraphView.tsx` + `components/graph/` — see the frontend
   architecture bullet: live force layout, eased camera with fling inertia, map-tile LOD,
   type-colored nodes via CSS tokens, click → evidence panel with citation chips, alias search
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
