# QuickJoiner

Onboarding intelligence for a new-joinee principal software developer. QuickJoiner connects
to an organization's systems (code, project management, wikis, CI/CD, deployments, log
platforms), **constantly learns** from them into a local vector memory, and answers questions
**only from what it has learned** — every answer cited, and "I haven't learned that yet"
when memory has nothing relevant.

- **Local-first** — LanceDB (embedded vectors) + SQLite (catalog **plus all config and
  connectors**) + local embeddings (fastembed). Your org's data stays on your machine, and there
  is no config file to hand-edit; tune everything from the UI. (Optional cloud mode swaps in
  Postgres + pgvector — see [Cloud mode](#cloud-mode-postgres--pgvector).)
- **Switchable LLM backend** — Anthropic Claude, Ollama (fully local), or **LiteLLM / any
  OpenAI-compatible proxy** (one endpoint in front of 100+ models). Token streaming and optional
  extended thinking on all three.
- **Multi-mode connectors** — pull APIs, push webhooks, live "read from source" agent tools,
  authenticated browser sessions with your own credentials, and scraping as a last resort.
  Types: files/URLs, git, GitHub, GitLab, Jira, Confluence, Azure DevOps (cloud **and**
  on-prem Server/TFS), Octopus Deploy, Grafana, Datadog, Dynatrace, Elasticsearch, and a
  generic web scraper.
- **Honesty by design** — a grounding contract forces citations and refusals, every refusal is
  captured as a **knowledge gap** with one-click remediation, and a **knowledge graph** links
  repos, packages, tickets, services, and environments so you can see how things connect.
- **Question autocomplete** — as you type, the composer suggests real questions drawn from what's
  actually been learned (your services, repos, environments, past questions). It needs no LLM, so
  it's instant, and it **keeps improving as you connect more systems** — each newly synced source
  adds its real entities to the suggestions automatically.

---

## Setup

Pick one of two paths. **Docker** is the fastest way to a running UI; the **local install**
is best if you want the `qj` CLI and to hack on the code.

### Prerequisites

| For… | You need |
|------|----------|
| Docker path | Docker Desktop (or Docker Engine + Compose) |
| Local path | Python **3.11+** and `git` |
| Building the web UI from source (optional) | Node.js **22+** |
| An answer to any question | one LLM backend: an **Anthropic API key**, a local **Ollama**, or a **LiteLLM / OpenAI-compatible** endpoint |

Notes:
- Ingesting and retrieval work with **no LLM and no cloud** (embeddings run locally via
  fastembed — a small model downloads once on first use, which needs internet that one time).
  You only need an LLM backend to have the agent *answer* questions.
- A prebuilt web UI ships with the app, so the Node toolchain is only needed if you want to
  change the frontend.

### Option A — Docker (recommended for a quick look)

```bash
git clone <this-repo> quickjoiner && cd quickjoiner
docker compose up -d --build           # builds UI + app, starts on http://localhost:8787
```

`docker compose up` boots with the **Ollama** provider by default (it reaches an Ollama running
on your host at `http://host.docker.internal:11434` — set that as the base URL in Settings).
To use Anthropic instead, edit `docker-compose.yml` (set `QJ_PROVIDER: anthropic` and pass
`ANTHROPIC_API_KEY`), or just change the provider in the UI's **⚙ Settings** after first boot.

All state (config, vectors, repo clones, model cache) persists in the `qj-data` volume. The image
is lean by default; add the browser used by the `web_scrape` fallback with
`docker build --build-arg WITH_BROWSER=1 .` (or uncomment the build args in the compose file).

### Option B — Local install (the `qj` CLI)

```powershell
git clone <this-repo> quickjoiner
cd quickjoiner

# 1. Create and activate a virtual environment (Python 3.11+)
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux

# 2. Install QuickJoiner (editable). Extras: [dev] tests, [browser] scraping, [cloud] Postgres
pip install -e .                    # or, faster: uv pip install -e .

# 3. Create the default workspace (~/.quickjoiner/default) and pick a provider
qj init                             # --provider anthropic | ollama | litellm  (default: anthropic)
```

Then wire up your chosen LLM backend (see [LLM backends](#llm-backends)) and go to
[First run](#first-run).

> The `qj` command lives in the venv, so keep it activated (or call `.venv\Scripts\qj.exe`).

---

## First run

No external systems required — teach a fact, then ask about it:

```powershell
qj learn "Deploys go out Tuesdays via Octopus; Priya owns the release calendar."
qj learn C:\work\platform-docs        # or ingest a folder / single file / URL

qj ask "How do deployments work here?" # grounded, cited answer (streams by default)
qj ask "What is the airspeed of a swallow?"   # -> "I haven't learned that yet."

qj status                             # what has been learned so far
qj chat                               # interactive, resumable session
qj serve                              # web UI + API on http://127.0.0.1:8787
```

`qj init` with no argument uses the **`default`** workspace, so the commands above need no extra
flags. To keep multiple orgs, `qj init acme` creates `~/.quickjoiner/acme`; select it later with
`--workspace` or by setting `QJ_WORKSPACE`.

---

## LLM backends

Choose one; you can also override per command with `qj ask --provider …`.

**Anthropic Claude** (best quality, cloud):
```powershell
qj init --provider anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or put it in <workspace>/.env
```

**Ollama** (fully local, private — no data leaves your machine):
```powershell
# install Ollama, then pull a tool-capable model, e.g.:  ollama pull llama3.1
qj init --provider ollama               # base URL defaults to http://localhost:11434
```

**LiteLLM / any OpenAI-compatible endpoint** (one gateway in front of OpenAI, Azure, Bedrock,
vLLM, …):
```powershell
qj init --provider litellm
# In ⚙ Settings (or via the API), set the LLM base URL to your proxy, e.g. http://localhost:4000,
# the model to one your proxy routes, and — if it needs a key:
$env:LITELLM_API_KEY = "sk-..."         # env var name is configurable (llm.api_key_env)
```
The secret is read from the environment at runtime and never written to config.

The provider is resilient to a flaky gateway: transient `401/429/5xx` responses and network
blips are retried with bounded exponential backoff (some proxies intermittently reject a valid
key under load), and tool names are auto-sanitized to the OpenAI-compatible pattern so
connector tools whose names contain spaces work with strict backends like gpt-oss. Live tool
results are capped (`chat.live_tool_result_max_chars`, default 24000) so a large source — e.g. a
big Octopus deployment dashboard — can't overflow the model's context window.

---

## Connect org systems

Register a source from the CLI (or use the **/connect** wizard in the web UI):

```powershell
qj connect git --name platform --option url=https://github.com/acme/platform.git --option token=env:GIT_TOKEN
qj connect github --name platform-gh --option repo=acme/platform --option token=env:GITHUB_TOKEN
qj connect jira --name pay-jira --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option projects=PAY,LEDG
qj connect confluence --name wiki --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option spaces=ENG
qj connect azure_devops --name ado --option organization=acme --option project=Payments --option token=env:AZURE_DEVOPS_PAT
# on-prem Azure DevOps Server / TFS: use server_url + collection instead of organization
# (verify_tls=false for a self-signed cert; api_version to match an older server):
qj connect azure_devops --name tfs --option server_url=https://tfs.company.com/tfs --option collection=DefaultCollection --option project=Payments --option token=env:AZURE_DEVOPS_PAT --option verify_tls=false
# Octopus paginates all projects (not just the first 100); incremental=true re-fetches a
# project's releases only when it changed since last sync (dashboard/projects always refresh):
qj connect octopus --name deploys --option server_url=https://octopus.acme.com --option api_key=env:OCTOPUS_API_KEY --option incremental=true

qj sync            # incremental pull from every source
qj sync pay-jira   # or just one source
qj test pay-jira   # credential / reachability check
```

Secrets use env indirection (`token=env:GITHUB_TOKEN`) — literal secrets are never stored.
The value is read from the process environment (or, conveniently, from a `<workspace>/.env`
file that QuickJoiner loads at startup). **Gotcha on Windows:** a variable you set via System
Properties → Environment Variables is only inherited by *newly started* processes, so a `qj`
command or `qj serve` already running won't see it — either restart the terminal / server, or
add the line to `<workspace>/.env` (e.g. `AZURE_DEVOPS_PAT=…`) which every `qj` invocation picks up.
Connectors also contribute **live tools** to the agent (GitHub code search / file read, Jira JQL,
Azure DevOps WIQL, log queries, deploy status), so questions like "what failed in CI last night?"
can be answered from the source directly, not only from ingested memory. QuickJoiner also
synthesizes a **dependency map** per repo (from package manifests) so cross-repo questions
("who consumes AppRiver.Nautical.Models?") resolve from either end.

---

## Web UI

```powershell
qj serve            # http://127.0.0.1:8787
```

A React app (Vite + TypeScript + Tailwind) with:
- **Streaming chat** with full-width answers — citations are inline superscripts (hover to see the
  source); each answer has a hover toolbar to **download it as markdown**, **view all cited sources**,
  and 👍/👎 — a 👎 asks what was wrong and **learns the correction into memory**. Refusals are shown
  as "not learned yet". Answers render full markdown (tables, task/nested lists, blockquotes, code,
  links, and live mermaid diagrams).
- **Projects and resumable conversations**, with automatic history compression.
- **Knowledge gaps** — a badge in the rail opens the knowledge-debt backlog (below).
- **Waypoints** — the interactive knowledge graph view.
- **⚙ Settings** — sign-in and per-user connector sharing, add/test/sync connectors (or the
  conversational `/connect` wizard), and tune workspace settings: LLM provider/model (incl. the
  **LiteLLM proxy URL + API-key env var**, with a **Test connection** button that pings the
  provider before you save), retrieval knobs (**hybrid** dense+sparse, **cross-encoder reranker**,
  **graph-expansion**, **contextual chunking**), **knowledge-graph** LLM triple extraction, chat
  compression, and embeddings. Toggles are labelled query-time (take effect immediately) vs
  ingest-time (need a re-sync to re-embed). Everything — config and connectors — lives in the
  workspace's SQLite `catalog.db`, not a YAML file.
- Composer commands: `/learn <fact>`, `/connect`, `/scrape <url>` (crawl → a cited report with
  mermaid diagrams you can then choose to learn).

The prebuilt UI is served automatically. To develop it:
`cd frontend && npm install && npm run dev` (proxies to `qj serve` on :8787); `npm run build`
emits `frontend/dist`, which `qj serve` picks up.

**Webhooks & scheduled syncs:** `POST /hooks/<source>` receivers verify GitHub
(`X-Hub-Signature-256`), GitLab (`X-Gitlab-Token`), or generic (`X-QJ-Signature`) HMAC signatures
against the source's `webhook_secret`; sources with a sync interval are refreshed by a background
scheduler.

---

## Knowledge-debt backlog (gaps)

Every refusal is a signal about what the org still needs to teach the tool. QuickJoiner logs each
`search_memory` miss, clusters similar refusals by topic, and suggests a fix:

- **Knowledge gaps** panel in the UI (open-count badge in the rail) lists clusters with one-click
  CTAs: **Connect** the suggested source (deep-links into the `/connect` wizard with the type
  preselected), **Teach** the answer, or **Dismiss**.
- `list_gaps` is also an agent tool — ask "what don't you know yet?".
- Privacy mode (`gaps.store_queries = false`) keeps only a hash of each query, so shared/cloud
  deployments never store raw question text.

API: `GET /api/gaps`, `POST /api/gaps/resolve`.

## Knowledge graph & cross-source correlation

The hard part of onboarding across 10–50 systems isn't finding one chunk — it's *joining* facts
across sources. QuickJoiner builds an entity/relationship graph during ingestion, so correlation
is a lookup rather than a bet on vector ranking:

- **Entities & edges** (repos, packages, services, environments, tickets, **symbols, modules**)
  from dependency maps, ticket-key references, issue/deploy metadata, **code structure**
  (`repo --defines--> symbol`, `repo --imports--> module`, per code file), and — optionally —
  **LLM-extracted relationships** over prose docs (`graph.extract_triples`, off by default; every
  edge validated against a fixed vocabulary and cited to its document).
- **Graph-expansion retrieval** (on by default): once an answer is grounded, QuickJoiner walks one
  hop out in the graph to surface linked evidence the vector search missed (a ticket → the repo that
  references it → the deploy that shipped it). It never changes the grounded-vs-refuse decision.
- Surfaced as the **Waypoints** graph view, agent tools `graph_neighbors` / `graph_path`, and
  `GET /api/graph` / `GET /api/graph/path`.

Retrieval quality is tuned for this too: **contextual chunking** prepends each chunk with its
`source · title · path` breadcrumb (and markdown sub-chunks keep their section heading) so a
chunk's embedding carries the context it was split away from, and a **cross-encoder reranker** runs
by default over the fused candidates (`retrieval.reranker`; `QJ_DISABLE_RERANKER=1` to turn it off).

Embeddings run locally via fastembed (`BAAI/bge-small-en-v1.5` by default; switch
`embedding.provider` to `ollama` to use `nomic-embed-text`). These models are trained for
*asymmetric* retrieval — a different task instruction on queries vs passages — which QuickJoiner
can apply via **`embedding.instruct`** (off by default; toggle in Settings → Embedding). It lifts
recall but shifts the score distribution, so re-check `retrieval.min_score` after enabling. Whether
this or an embedding-model swap actually helps is a *measure-it* question — run the
`docs/plans/05` A/B matrix on your real corpus before changing the default.

---

## Briefs, exports, evals

```powershell
qj brief quick-wins                    # also: architecture | week1 | roadmap (cited, saved, re-ingested)
qj agents-md <source>                  # per-repo architecture brief for principal engineers/
                                        #   architects, generated from the repo's file tree +
                                        #   code-graph facts + dependency map + retrieved docs
                                        #   (never runs code). Refines a real AGENTS.md if the
                                        #   repo already has one, instead of overwriting it.
                                        #   Saved to <workspace>/generated/<source>/AGENTS.md
                                        #   and re-ingested — never written into the repo itself.
                                        #   Also available per-connector in the web UI
                                        #   ("Architecture brief" button), and can auto-generate
                                        #   once on first sync (Settings → Repositories toggle).
qj ask "deploy inventory?" -f pptx     # export answers: md | html | csv | pptx
qj eval my-evals.yaml --init           # write a starter eval set
qj eval my-evals.yaml [--agent]        # retrieval metrics (recall@k, MRR, refusal accuracy, hop_coverage);
                                       # --agent adds end-to-end citation/refusal checks
qj eval docs/evals/multi-hop-crosssource.yaml   # shipped multi-hop / cross-source eval set
qj browser login https://sso.acme.com  # persistent Playwright profile (install: pip install -e ".[browser]")
```

## Cloud mode (Postgres + pgvector)

By default QuickJoiner is entirely on-prem (SQLite + LanceDB files). Set `DATABASE_URL` to a
`postgres://…` DSN and it transparently switches to a Postgres catalog + pgvector store instead —
no code changes. Install the extra and run the cloud compose file:

```bash
pip install -e ".[cloud]"
docker compose -f docker-compose.cloud.yml up -d   # app + pgvector
```

---

## Architecture

```
connectors (files/url, git, GitHub, GitLab, Azure DevOps, Jira, Confluence,
            Octopus, Grafana, Datadog, Dynatrace, Elastic, web_scrape)
   ├─ sync() -> Documents          (pull; incremental via per-source sync state)
   ├─ handle_event(payload)        (push; POST /hooks/<source>, HMAC-verified)
   ├─ tools() -> live agent tools  (JQL, WIQL, code search, log queries, deploy status)
   └─ ingest pipeline: normalize -> sha256 dedupe -> content-aware chunking -> embeddings
        ├─ vectors   LanceDB (on-prem)  /  pgvector (cloud)
        ├─ catalog   SQLite  (on-prem)  /  Postgres (cloud)   — config, sources, graph, gaps
        └─ sparse    SQLite FTS5        /  Postgres tsvector  — hybrid retrieval
agent: grounded tool-calling loop (search_memory / remember / list_gaps / graph_* / connectors)
   └─ LLM provider: Anthropic  <->  Ollama  <->  LiteLLM (OpenAI-compatible); streaming + thinking
api: FastAPI — SSE /api/chat, sources, sync, search, briefs, gaps, graph + React web UI
cli: qj init|learn|connect|sync|test|ask|chat|projects|sessions|serve|status|sources|
     brief|eval|browser|users|login|logout|whoami
```

## Grounding contract

The agent must call `search_memory` before answering org-specific questions, cite each claim as
`[title or uri]`, and refuse to invent org facts. Retrieval below `retrieval.min_score` returns
`NO_RESULTS`, which the agent reports as "I haven't learned that yet" plus a suggestion of which
source to connect (and logs a knowledge gap).

Search is **hybrid** by default: a dense cosine leg plus a sparse exact-token leg (BM25), fused
with reciprocal rank fusion, then reordered by a cross-encoder reranker (on by default,
`retrieval.reranker`). The sparse leg rescues error codes, ticket IDs, and service names that
embeddings rank poorly; graph-expansion then adds cross-source leads. Through all of it the
grounding gate stays on the **dense cosine score** alone — fusion, reranking, and graph expansion
change what surfaces and in what order, but never turn a refusal into an invented answer.

## Development

```powershell
pip install -e ".[dev]"
pytest                                 # or: .venv\Scripts\python.exe -m pytest -q
```

Machine-specific dev notes (venv layout, building the frontend, the live Ollama/Playwright
setup) and the full design/roadmap live in `CLAUDE.md` and `docs/`.

## Status

Core memory/agent, the connector framework + 13 connectors, FastAPI/UI/scheduler/webhooks,
briefs + browser fallback + evals, hybrid retrieval, the knowledge graph, the knowledge-debt
backlog, and cloud (Postgres/pgvector) groundwork are all in place. See `CLAUDE.md` for detailed
status, conventions, and next steps (an eval-driven fine-tuning decision on a real corpus).
