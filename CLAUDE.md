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

## Commands

```
qj init <org> [--provider anthropic|ollama]   # create workspace (~/.quickjoiner/<org>)
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

Workspace layout: `config.yaml`, `catalog.db` (SQLite), `lancedb/` (vectors), `repos/` (git clones).
Override location with `--workspace` or `QJ_WORKSPACE`.

## Architecture

- `quickjoiner/llm/` — provider abstraction. **Neutral message format** documented in
  `llm/base.py` (`user` / `assistant`+tool_calls / `tool` dicts); each provider converts to its
  wire format. Anthropic default model: `claude-opus-4-8`. Ollama via native `/api/chat`.
  Both providers stream via `chat(..., on_stream=(kind, delta))` with kind `text|thinking`;
  extended thinking is config-gated (`llm.thinking`, `llm.thinking_budget`). Anthropic signed
  thinking blocks ride on assistant history messages as `thinking_blocks` and are re-emitted
  FIRST in `_to_wire` (API requirement during tool use).
- `quickjoiner/memory/` — `store.py` (LanceDB, cosine; score = 1 − distance), `catalog.py`
  (SQLite: sources, document hashes, sync state), `embedder.py` (fastembed bge-small default).
- `quickjoiner/ingest/` — `pipeline.py` (sha256 dedupe → chunk → embed → upsert; idempotent),
  `chunkers.py` (markdown/code/prose aware).
- `quickjoiner/connectors/` — contract in `base.py`: `test()`, `sync(state) -> Iterator[Document]`,
  `tools() -> [AgentTool]` (live agent tools), `handle_event(payload)` (webhooks), and a
  `modes` flag (PULL/PUSH/LIVE/BROWSER/SCRAPE). Register with `@register`; add new imports to
  `registry._load_builtin_connectors`. Payload→Document converters are **module-level pure
  functions** so tests can hit them without HTTP mocking (see `tests/test_connectors.py`,
  `tests/test_phase4_connectors.py`). `logsearch/` (grafana/datadog/dynatrace/elastic) ingests
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
  (search_memory/remember/list_sources in `tools.py`), tool-call loop (`agent.py`, max 10 rounds),
  onboarding briefs (`briefs.py`: seed queries → retrieved chunks → one-shot LLM call → saved to
  `<workspace>/briefs/` and re-ingested; refuses without hits and without building a provider).
- `quickjoiner/auth.py` — opt-in local auth. `Auth` over the catalog: PBKDF2 password hashing,
  bearer tokens (sha256-hashed at rest in `auth_tokens`), `users` table. **Open mode until the
  first user exists** (no login, everything shared = pre-auth behavior). Sharing model on
  `SourceConfig` (`owner`, `shared`): ownerless = commons; owned = owner-only unless `shared`.
  `visible()` / `can_manage()` are the gate. Ingested *knowledge* stays one communal memory;
  sharing governs who sees/manages a **connector's config + credentials** and gets its live tools.
- `quickjoiner/api/` — FastAPI (`app.py`: SSE `/api/chat`, sources, sync, search, briefs;
  `/api/auth/*` status/users/login/logout; `/api/connectors` CRUD + `/test` + `/types`) +
  `hooks.py` (HMAC-verified `POST /hooks/{source}` push ingestion). Bearer token via
  `Authorization` header → `_user()`; secret option values masked (`MASKED`) in responses, and a
  PATCH sending the mask back keeps the stored secret. Live connector tools in chat are scoped to
  `ctx.visible_sources(user)`. UI: `api/static/index.html` — the chat "logbook" plus a top-right
  **Settings drawer** (account sign-in/out + connector registry plates with capability stamps,
  test/sync/share/remove, and a dynamic per-type add-connector form built from `/api/connectors/types`).
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

## Next steps (agreed with user)

1. **Fine-tuning decision is DEFERRED pending evals on a real corpus**: connect a real org (or
   large OSS repo), write ~50 eval cases, run `qj eval`. Recommendation already given: no SLM,
   no knowledge fine-tune (breaks freshness/citations/refusal); if quality gaps appear, try
   reranker/hybrid search first, then embedding fine-tune (highest ROI), and optionally a
   behavior-tune of a small Ollama model for tool-calling discipline.
2. ~~First live LLM run~~ DONE 2026-07-07 via Ollama gemma4:cloud (grounded+cited answer and
   refusal both verified live). Remaining: the multi-hop "Jira ticket implemented+deployed?"
   scenario needs real connectors — add it as an eval case once an org is connected.

Known gaps: Anthropic live path untested (no API key; Ollama path verified live),
connectors untested against real external services (converters covered by unit tests only).
