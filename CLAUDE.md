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
    `%USERPROFILE%\.local\bin\uv.exe pip install -e ".[dev]" --python .venv\Scripts\python.exe`
- No `ANTHROPIC_API_KEY` / Ollama on this machine so far — live LLM calls can't be exercised;
  the agent loop is covered by scripted-provider tests (`tests/test_agent_loop.py`,
  `tests/test_streaming.py`). Playwright is NOT installed (browser extra + `playwright install
  chromium` needed before `qj browser login` works).

## Commands

```
qj init <org> [--provider anthropic|ollama]   # create workspace (~/.quickjoiner/<org>)
qj learn <path|url|"free text fact">          # ad-hoc ingestion / taught notes
qj connect <type> --name N -o key=value ...   # register a source (types: files git github gitlab
                                              #   jira confluence azure_devops octopus grafana
                                              #   datadog dynatrace elastic web_scrape)
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
  `web_scrape` connector (`scraper.py`, injectable `_fetch_http` for tests).
- `quickjoiner/agent/` — grounded system prompt (`prompts.py`), built-in tools
  (search_memory/remember/list_sources in `tools.py`), tool-call loop (`agent.py`, max 10 rounds),
  onboarding briefs (`briefs.py`: seed queries → retrieved chunks → one-shot LLM call → saved to
  `<workspace>/briefs/` and re-ingested; refuses without hits and without building a provider).
- `quickjoiner/api/` — FastAPI (`app.py`: SSE `/api/chat`, sources, sync, search, briefs) +
  `hooks.py` (HMAC-verified `POST /hooks/{source}` push ingestion). UI: `api/static/index.html`.
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
2. First live LLM run (needs API key or Ollama): validate chained flow end-to-end; add the
   "Jira ticket implemented+deployed?" scenario as an eval case.

Known gaps: live LLM path untested (no API key), Playwright untested (not installed),
connectors untested against real external services (converters covered by unit tests only).
