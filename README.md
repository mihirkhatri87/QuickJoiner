# QuickJoiner

Onboarding intelligence for a new-joinee principal software developer. QuickJoiner connects
to an organization's systems (code, project management, wikis, CI/CD, deployments, log
platforms), **constantly learns** from them into a local vector memory, and answers questions
**only from what it has learned** — every answer cited, and "I haven't learned that yet"
when memory has nothing relevant.

- **Local-first**: LanceDB (embedded vectors) + SQLite (catalog **+ all config and connectors**) + local embeddings (fastembed). Org data stays on your machine, and there's no config file to hand-edit — tune everything from the UI.
- **Switchable LLM backend**: Anthropic Claude API or Ollama (fully local inference), with token streaming and optional extended thinking.
- **Multi-mode connectors**: pull APIs, push webhooks, live "read from source" agent tools, authenticated browser sessions with your own credentials, and scraping as a last resort.
- **Connectors**: files/URLs, git repos, GitHub, GitLab, Jira, Confluence, Azure DevOps, Octopus Deploy, Grafana, Datadog, Dynatrace, Elasticsearch, and a generic web scraper.

## Install

```powershell
# from the repo root (Python 3.11+)
pip install -e .          # or: uv pip install -e .
```

## Quick start

```powershell
qj init acme --provider anthropic     # or: ollama (fully local) / litellm (OpenAI-compatible proxy)
$env:ANTHROPIC_API_KEY = "sk-ant-..." # or put it in <workspace>/.env
$env:QJ_WORKSPACE = "$HOME/.quickjoiner/acme"
# LiteLLM: set llm.base_url to your proxy (e.g. http://localhost:4000) + $env:LITELLM_API_KEY if it needs a key

qj learn C:\work\platform-docs        # ingest a folder (or a single file / URL)
qj learn "Deploys go out Tuesdays via Octopus; Priya owns the release calendar."

qj ask "How do deployments work here?"
qj chat                               # interactive session
qj status                             # what has been learned so far
qj ask "..." --provider ollama        # switch inference backend per command
```

## Connect org systems

```powershell
qj connect git --name platform --option url=https://github.com/acme/platform.git --option token=env:GIT_TOKEN
qj connect github --name platform-gh --option repo=acme/platform --option token=env:GITHUB_TOKEN
qj connect jira --name pay-jira --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option projects=PAY,LEDG
qj connect confluence --name wiki --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option spaces=ENG
qj connect azure_devops --name ado --option organization=acme --option project=Payments --option token=env:AZURE_DEVOPS_PAT

qj sync            # incremental pull from every source
qj sync pay-jira   # or one source
qj test pay-jira   # credential/reachability check
```

Connectors also contribute **live tools** to the agent (GitHub code search/file read,
Jira JQL, Azure DevOps WIQL), so questions like "what failed in CI last night?" can be
answered from the source directly, not just from ingested memory.

## Web UI, webhooks, scheduled syncs

```powershell
qj serve                    # http://127.0.0.1:8787 — streaming chat (with thinking),
                            # sources dashboard, POST /hooks/<source> webhook receivers
```

The web UI is a React app (`frontend/` — Vite + TypeScript + Tailwind): streaming chat with a
provenance ledger (every grounded answer lists its numbered sources in the margin), projects and
resumable conversations, and a **⚙ Settings** drawer that manages everything without touching a
file: sign-in and per-user connector sharing, adding/testing/syncing connectors, and tuning
**workspace settings** (LLM provider/model, retrieval threshold, chat compression, embeddings).
All of it — config and connectors — is stored in the workspace's SQLite `catalog.db`, not a YAML
file. To develop the UI: `cd frontend && npm install && npm run dev` (proxies to `qj serve` on
:8787); `npm run build` emits `frontend/dist`, which `qj serve` picks up automatically.

Webhooks verify GitHub (X-Hub-Signature-256), GitLab (X-Gitlab-Token), or generic
(X-QJ-Signature) HMAC signatures against the source's `webhook_secret`.

## Run with Docker

```bash
docker compose up -d                    # build + run; UI on http://localhost:8787
# or without compose:
docker build -t quickjoiner .
docker run -d -p 8787:8787 -v qj-data:/data quickjoiner
```

Everything persists in the `/data` volume (config, vectors, repo clones, embedding-model cache).
Set the first-boot provider with `QJ_PROVIDER` (default `anthropic`; pass `ANTHROPIC_API_KEY` for
it, or use `ollama` and point the base URL at `host.docker.internal:11434` in Settings); after
first boot, tune everything from the UI. The default image is lean — build with
`--build-arg WITH_BROWSER=1` to bundle Playwright for the `web_scrape` browser fallback.

## Briefs, exports, evals

```powershell
qj brief quick-wins                    # also: architecture | week1 | roadmap (cited, saved, re-ingested)
qj ask "deploy inventory?" -f pptx     # export answers: md | html | csv | pptx
qj eval my-evals.yaml --init           # write a starter eval set
qj eval my-evals.yaml [--agent]        # retrieval metrics (recall@k, MRR, refusal accuracy);
                                       # --agent adds end-to-end citation/refusal checks
qj browser login https://sso.acme.com  # persistent Playwright profile (install: pip install -e .[browser])
```

## Architecture

```
connectors (files/url, git, GitHub, GitLab, Azure DevOps, Jira, Confluence,
            Octopus, Grafana, Datadog, Dynatrace, Elastic, web_scrape)
   ├─ sync() -> Documents          (pull; incremental via per-source sync state)
   ├─ handle_event(payload)        (push; POST /hooks/<source>, HMAC-verified)
   ├─ tools() -> live agent tools  (JQL, WIQL, code search, log queries, deploy status)
   └─ ingest pipeline: normalize -> sha256 dedupe -> content-aware chunking -> embeddings
        ├─ LanceDB  (vector chunks)        <workspace>/lancedb/
        ├─ SQLite   (catalog + sync state) <workspace>/catalog.db
        └─ SQLite   (FTS5 sparse index)    <workspace>/fts.db
agent: tool-calling loop, max 10 chained rounds (search_memory / remember / connector tools)
   └─ LLM provider: Anthropic Claude API <-> Ollama; streaming + extended thinking
api: FastAPI — SSE /api/chat, sources, sync, search, briefs + React web UI (frontend/dist)
cli: qj init|learn|connect|sync|test|ask|chat|serve|status|sources|brief|eval|browser
```

## Grounding contract

The agent must call `search_memory` before answering org-specific questions, cite each
claim as `[title or uri]`, and refuse to invent org facts. Retrieval below
`retrieval.min_score` returns `NO_RESULTS`, which the agent must report as
"I haven't learned that yet" plus a suggestion of which source to connect.

Search is hybrid by default: a dense cosine leg plus a sparse exact-token leg (BM25),
fused with reciprocal rank fusion, with an optional cross-encoder reranker
(`retrieval.reranker = "fastembed"`). The sparse leg rescues error codes, ticket IDs
and service names that embeddings rank poorly — but the grounding gate above stays
on the dense cosine score, so hybrid never changes a refusal into an invented answer.

## Tests

```powershell
pip install -e .[dev]
pytest
```

## Status

All four planned phases are complete (core memory/agent, connector framework + 13 connectors,
FastAPI/UI/scheduler/webhooks, briefs + browser fallback + evals). See `CLAUDE.md` for the
detailed status, conventions, and next steps (eval-driven fine-tuning decision).
