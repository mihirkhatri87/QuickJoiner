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
  on-prem Server/TFS), Octopus Deploy, **OneDrive for Business / SharePoint**, Grafana, Datadog,
  Dynatrace, Elasticsearch, and a generic web scraper.
- **OneDrive / SharePoint, with your own sign-in** — sign in with your Microsoft 365 account
  (in the UI, or `qj onedrive login` for a terminal/Docker) and QuickJoiner can read exactly what
  *you* can read, including documents other people shared with you. It **never crawls your drive**:
  you point it at a document — paste any link you can open, or say
  `/qj learn from this onedrive document <url>` in chat — and only that is learned. Word,
  PowerPoint, Excel, PDF, CSV, Markdown, **zip archives** and many other text formats are all
  extracted; a later sync keeps what you taught it up to date without going looking for more.
  ⚠ Note: a OneDrive connector you keep **private** (the signed-in default) is now yours alone
  — nobody else can search, cite, or even see its documents' names in autocomplete or the graph.
  Use `--share` (CLI) or the share toggle on the connector to open it to the workspace. See
  **Who can read what** below.
- **Ask about a file (per-question attachments)** — click 📎 or drag files onto the chat to attach
  **Word, PowerPoint, Excel, PDF, Markdown, text, JSON, HTML, or code** as *context for that
  question*. Slide decks are read properly — labels inside **grouped diagrams**, SmartArt and
  chart titles all come through, not just top-level text boxes. The file's text is extracted and used to answer, and it shows up with a download link
  right under your question. These are **ephemeral and private to the conversation** — never added
  to learned memory — and are auto-deleted after 7 days (after which the history shows the filename
  with a "deleted" warning instead of a link). Want to keep one? Flip the **"Learning permanently"**
  toggle beside the staged file before sending (or just ask — "learn this document" works now), and
  it is also ingested into memory as a cited, searchable document. To learn a file already on disk without attaching it, type **`/ingest <file path>`** — a deterministic command, no LLM involved.
- **Document uploads into memory** — to teach QuickJoiner a document *permanently*, ingest it into
  the rolling **Uploads** connector — `POST /api/uploads`, or ask in chat (`/qj ingest C:\docs\spec.pdf`).
  Text is extracted at ingest and the file becomes cited memory. One continuously-growing connector,
  no new connector per file, re-syncable and cleanable like any other. **Zip archives** are
  expanded and their readable members ingested with their paths kept. (Text-only today; **images
  are not read yet** — an image says so explicitly rather than being ignored, and reading
  images/scanned pages is on the roadmap.)
- **See what it learned, label it, and ask within it** — click any connected system to browse
  the documents it actually ingested, grouped by folder. Tag a whole connector, a folder, or a
  single document — a **folder tag covers files added to it later**, so you tag once. `aka`
  gives something an alternate name ("the Zix roadmap"). Then use the scope chip beside the
  composer to limit a question to one connector, a tag, or specific documents: the search is
  filtered before it runs and connectors you excluded can't be queried at all, so answers come
  back faster and with less ambiguity. Leave it on **All memory** and nothing changes.
- **Honesty by design** — a grounding contract forces citations and refusals, every refusal is
  captured as a **knowledge gap** with one-click remediation, and a **knowledge graph** links
  repos, packages, tickets, services, environments, **message topics, and datastores** so you
  can see how things connect — including pub/sub coupling (`publishes_to`/`subscribes_to`) and
  data residence (`stores_in`), the runtime relationships package manifests can never reveal
  ("if I change this event, who breaks?"). These are extracted **deterministically** from Service
  Bus SDK calls across C#/Python/JS/Java/Go, app config (appsettings, .config, Spring), connection
  strings, and CloudFormation/serverless templates — no LLM required, and config-substitution
  placeholders (Octopus `#{Var}`, env vars) are never guessed at.
- **Multi-angle answers with honest confidence** — when the graph has recorded more than one
  materially different connection between two things, the agent surfaces *all* of them (or asks
  one clarifying question) instead of silently picking the shortest; every relationship carries a
  **server-computed confidence** (evidence shape + corroboration — informal meeting notes score
  below a real architecture doc), and asking for "options" renders a ranked candidate carousel
  with shared citations.
- **Cross-source identity bridges** — the same real-world thing often appears under different
  entity types per system (the `connector` git repo, the `Connector` Octopus service, the pipeline
  that builds it). QuickJoiner links them with deterministic `same_as` bridges — from exact name
  matches and from every connector's **"also known as"** field (comma-separated aliases you declare
  at setup on any connected system, e.g. the Stevedore repo aka `appriver.provisioning`; like the
  connector name, it locks after the first sync) — so multi-hop questions can cross systems
  ("which environment is this repo's code deployed to?"). Bridges are honestly labeled as
  name-equality inferences, scored low-confidence, and never affect the answer/refuse decision.
- **Question autocomplete** — as you type, the composer suggests real questions drawn from what's
  actually been learned (your services, repos, environments, past questions). It needs no LLM, so
  it's instant, and it **keeps improving as you connect more systems** — each newly synced source
  adds its real entities to the suggestions automatically.
- **Sync control** — syncs run as background jobs you can **watch live** (streaming logs),
  **pause/resume**, and **stop** at any time; stopping asks whether to clean up the partial pull.
  The live log shows the **current stage and an estimated %** (e.g. "reading files 40/76 · 53%",
  or per-team progress for a large TFS pull), and stop/pause take effect within seconds even mid-pull
  — the pipelines check for it inside their loops, not just between documents. Multiple sources sync
  at once, and a **clean re-sync** (`qj resync`, or the button in the UI) purges a source's
  documents, vectors and graph edges before re-pulling, so the knowledge graph never ends up
  corrupted or half-populated. **A network drop won't lose a sync** — if the connection fails
  mid-ingest, the job doesn't error out: it auto-pauses and **retries itself on a growing delay
  (20s, 40s, 80s… for up to an hour)**, picking up from where it left off (already-ingested data
  is never re-fetched wastefully). If the network still hasn't recovered after an hour it stays
  paused with a clear message — hit **Resume** once you're back online, or **Stop** to abandon the
  run. (Auth or configuration errors still fail fast — retrying those would be pointless.)
  **A paused sync survives a restart** — pause a sync, close your laptop, and it's still there
  (paused) when you reopen; hit **Resume** and it picks up from the last checkpoint. (Because a
  killed process can't literally continue an in-progress pull, resuming re-runs the connector from
  where it last committed — already-ingested data is skipped, so nothing is duplicated or lost.)
  The log panel never traps you: close it and the sync keeps
  running, with a live indicator on the source (in the sidebar and on its connector card) that
  re-opens the log. **Reloading the page doesn't lose a sync** — jobs live on the server, so the
  UI picks them straight back up. The bell menu's **"View full history by connector"** shows every
  sync over the last 7 days grouped by connector, with the latest run's live %.
- **Clean up / delete without leftovers** — a connector's **name is its identity**: everything it
  learns is stored under `type:name`, so it can only be renamed **before its first sync** (while it
  has 0 documents and nothing is keyed to it yet); the edit view lets you change the name then, and
  locks it with the reason afterwards. **Clean up** forgets a source's documents, vectors and graph
  edges while keeping it connected, and **deleting** a connector runs that cleanup automatically —
  otherwise its knowledge would linger forever with nothing able to re-sync or remove it. Both run
  as background jobs with a live log, and both refuse to start while that source is mid-sync. Need
  the old behaviour? `DELETE /api/connectors/<name>?keep_memory=true` retires the connector and
  keeps what it taught.
- **Reset all memory** — Settings has a **Danger zone** with a type-to-confirm "Reset all learned
  memory" that wipes every ingested document, its vectors, and the whole knowledge graph, and clears
  each connector's sync watermark — the workspace goes back to as if nothing had ever synced, while
  your **connectors stay configured** (re-sync any time). Chat history and settings are untouched.
  It runs as a background job with a **live log** and shows in the activity bell + sync history, so
  you can watch it wipe and confirm it finished. (`POST /api/memory/reset`; refuses while a sync is
  running.)
- **Activity menu** — the bell in the top bar is the 24-hour view of what the workspace has been
  doing: syncs running right now and everything that finished, with counts or the error. It badges
  what you haven't seen yet, keeps unseen entries visually distinct from ones you've already read,
  and survives both a page reload and a server restart.

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
This same flag also covers credential-gated sites: a container has no display, so signing in
opens a **live remote browser view in the UI** instead of a local window — see "Ingesting a
site behind a login" below. No extra packages or ports needed for that; it rides the same
headless Chromium.

⚠️ **Give Docker enough memory if you crawl with the browser.** Chromium is the single
hungriest thing QuickJoiner runs — a real crawl was measured at **~1.2 GB across 13 Chromium
processes**. If Docker's VM is small (~2 GB is a common effective allocation, regardless of how
much RAM the machine has), a crawl exhausts it and the container is killed mid-sync: every run
then shows up as *"interrupted — server restarted before it finished"*, and the log panel
reports a "network error" that is really just its stream being cut. Under real starvation the
Docker *daemon* can fall over too, failing builds with an `EOF`/500. **Check what you have —
`docker info --format "{{.MemTotal}}"` — and give it 4–8 GB** (Docker Desktop → Settings →
Resources → Memory, or a `[wsl2]` / `memory=8GB` block in `%USERPROFILE%\.wslconfig` followed by
`wsl --shutdown`). The compose file already sets `shm_size: 1gb`, because Docker's default 64 MB
`/dev/shm` also crashes Chromium tabs.

**Rebuilds are incremental.** The Dockerfile installs dependencies (and the ~250MB Chromium)
from `pyproject.toml` alone, *before* copying any application code, so editing Python or
frontend source doesn't re-resolve dependencies or re-download the browser. In practice a
one-line Python change rebuilds in **~10s**, a frontend change in ~30s (dominated by the Vite
build), and a no-op build in ~3s — versus ~5 minutes when a source edit invalidated the
dependency layers. If you ever *do* need a genuinely clean build, use
`docker compose build --no-cache`.

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
qj learn --share "Release calendar lives in Confluence."   # teach EVERYONE, not just you
qj learn C:\work\platform-docs        # ingest a folder / single file / URL
qj learn C:\work\Onboarding.docx      # Word/PowerPoint/Excel/PDF/Markdown are extracted too

qj ask "How do deployments work here?" # grounded, cited answer (streams by default)
qj ask "What is the airspeed of a swallow?"   # -> "I haven't learned that yet."

qj status                             # what has been learned so far
qj chat                               # interactive, resumable session
qj serve                              # web UI + API on http://127.0.0.1:8787
```

`qj init` with no argument uses the **`default`** workspace, so the commands above need no extra
flags. To keep multiple orgs, `qj init acme` creates `~/.quickjoiner/acme`; select it later with
`--workspace` or by setting `QJ_WORKSPACE`.

### API docs & runbooks

`qj serve` exposes a REST API alongside the UI. Interactive, **function-grouped** API docs are at
**`/docs`** (Swagger UI) and **`/redoc`**, with the raw schema at **`/openapi.json`** — every
endpoint has a plain-English summary. For a hands-on, end-to-end walkthrough (connect → sync →
query), import the ready-made **Postman** and **Bruno** collections in [`docs/api/`](docs/api/) —
all common config (base URL, connector, queries) lives in one variables file. *(Swagger UI loads
its assets from a CDN, so `/docs` is blank on an air-gapped host; `/openapi.json` always works.)*

---

## LLM backends

Choose one; you can also override per command with `qj ask --provider …`.

**Anthropic Claude** (best quality, cloud):
```powershell
qj init --provider anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or put it in <workspace>/.env
```
Prompt caching is on by default (`llm.prompt_cache`): the system prompt + tool specs and the
growing conversation are cached server-side, so the 2nd..Nth round of every tool-using answer
and every follow-up turn read the prompt at ~10% of the input price (and faster). Disable it
only if a fronting proxy rejects the `cache_control` field.

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

For reasoning models behind the proxy (gpt-oss and friends) there are dedicated knobs and
behaviors, all safe on backends that don't support them (a rejecting 400 is stripped and
retried automatically):
- the chain of thought that produced a tool call is passed back (`reasoning_content`) until the
  turn completes — the Harmony-format contract that keeps gpt-oss from re-planning every round;
- `llm.reasoning_effort` (`low`/`medium`/`high`) — the big cost/latency dial;
- `llm.parallel_tool_calls: false` forces one tool call per turn if your backend emits garbled
  parallel calls;
- streamed responses request usage reporting, and token counts (including the backend's
  prefix-cache hits, `cached_tokens`) are logged at DEBUG level;
- `llm.proxy_cache_control` (off by default) forwards an Anthropic-style cache marker for
  proxies that route to Claude.

The provider is resilient to a flaky gateway: transient `401/429/5xx` responses and network
blips are retried with bounded exponential backoff honoring `Retry-After` (some proxies
intermittently reject a valid key under load), one pooled HTTP connection is reused across a
whole tool loop, and tool names are auto-sanitized to the OpenAI-compatible pattern so
connector tools whose names contain spaces work with strict backends like gpt-oss. Live tool
results are capped (`chat.live_tool_result_max_chars`, default 24000) so a large source — e.g. a
big Octopus deployment dashboard — can't overflow the model's context window. Tool specs are
sent in a deterministic (name-sorted) order on every provider, so backends with automatic
prefix caching (OpenAI-compatible proxies, llama.cpp/Ollama KV-cache reuse) get cache hits
across turns too.

---

## Connect org systems

Register a source from the CLI (or just say **`/qj connect a <type> …`** in the web UI):

```powershell
qj connect git --name platform --option url=https://github.com/acme/platform.git --option token=env:GIT_TOKEN
# ^ if the URL is a GitHub/GitLab repo, qj offers to ALSO wire up the matching API connector
#   (merge requests, issues, pipelines + the ticket<->MR graph) — one prompt, no retyping.
qj connect github --name platform-gh --option repo=acme/platform --option token=env:GITHUB_TOKEN
qj connect gitlab --name platform-gl --option project=grp/subgrp/platform --option token=env:GITLAB_TOKEN
# GitLab MRs are linked in the knowledge graph to their repo + source branch, keyed by name — so
# they merge with TFS work-item "Development" links (same repo + branch name, group nesting aside)
# and you can ask "what's the MR for this TFS issue?". The agent also answers live, read-only
# questions the API exposes but memory can't — "list all open MRs", "who reviewed !88", "did the
# build for branch X pass (and link)", "which jobs failed", "compare develop..feature/x", "recent
# commits by <author>", "project languages/default branch". GitHub gets the same tool family; ADO
# adds work-item detail + build details/logs. All READ-ONLY (nothing is ever changed remotely).
# Leave --option token off to use $GITLAB_TOKEN.
# Org-specific rules (opt-in, off by default): --option ticket_in_branch=true links each MR/branch
# straight to its TFS ticket when the work-item id is in the branch name (e.g. feature/team/321135-x
# -> ticket #321135; override the pattern with --option ticket_pattern="(\d{4,})"). And if your
# pipelines mirror branches into TFS, --option tfs_sync_stage=<sync job/stage name> captures, per
# branch, the most recent successful TFS-sync pipeline (--option tfs_sync_lookback_days=15).
qj connect jira --name pay-jira --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option projects=PAY,LEDG
qj connect confluence --name wiki --option base_url=https://acme.atlassian.net --option email=me@acme.com --option api_token=env:JIRA_API_TOKEN --option spaces=ENG
qj connect azure_devops --name ado --option organization=acme --option project=Payments --option token=env:AZURE_DEVOPS_PAT
# Work items ingest by team over recent sprints (default last 10); restrict teams and set the
# window with --option teams=... --option sprints=...  Build pipelines are mapped to the repos
# they build and recent builds are recorded per source branch (a GitLab<->TFS bridge when branches
# mirror into TFS) — ask "did branch X build?" and the agent checks TFS live. PRs are not ingested.
# Each work item's "Development" links (commits/branches/PRs) are turned into graph edges
# (ticket -> implemented_in -> repo, ticket -> on_branch -> branch), keyed by repo/branch NAME so
# they join the GitLab side — the basis for "what's the MR for this TFS issue?" (needs a clean re-sync).
# on-prem Azure DevOps Server / TFS: use server_url + collection instead of organization
# (verify_tls=false for a self-signed cert; api_version to match an older server):
qj connect azure_devops --name tfs --option server_url=https://tfs.company.com/tfs --option collection=DefaultCollection --option project=Payments --option token=env:AZURE_DEVOPS_PAT --option teams="Payments Team,Platform Team" --option sprints=10
# Octopus paginates all projects (not just the first 100); incremental=true re-fetches a
# project's releases only when it changed since last sync (dashboard/projects always refresh):
qj connect octopus --name deploys --option server_url=https://octopus.acme.com --option api_key=env:OCTOPUS_API_KEY --option incremental=true

# OneDrive for Business / SharePoint — YOUR sign-in, and nothing is crawled.
# You need one value from your organisation's Azure app registration: the Application
# (client) ID. Ask "may reach" for only what you need — my_drive needs Files.Read (usually
# self-service), shared_with_me needs Files.Read.All, sharepoint needs Sites.Read.All
# (usually admin consent).
qj connect onedrive --name my-files --option client_id=00000000-0000-0000-0000-000000000000 --option access="my_drive,shared_with_me"
qj onedrive login my-files      # device-code sign-in: type a short code at microsoft.com/devicelogin
qj onedrive status my-files     # who it is signed in as, and how much it has learned
# Learn specific documents — a link you can open, or a path in your own drive. A folder
# learns the readable files under it. This is the ONLY way documents get in:
qj onedrive learn my-files "https://contoso.sharepoint.com/:w:/r/sites/Eng/Shared%20Documents/design.docx"
qj onedrive learn my-files "Projects/Alpha/notes.md" "Projects/Alpha/specs"
# In chat you can say the same thing in words:  /qj learn from this onedrive document <url>
# A later `qj sync my-files` refreshes those documents. It never goes looking for new ones.

qj sync            # incremental pull from every source
qj sync pay-jira   # or just one source
qj resync pay-jira # purge that source (docs, vectors, graph) and re-sync from scratch
qj test pay-jira   # credential / reachability check
qj regraph         # rebuild the knowledge graph from documents already ingested —
qj regraph pay-jira #  no re-fetch, no re-chunk, no re-embed (add --with-triples to
                   #  also re-mine LLM relationships: one model call per document)
# A plain `sync` never re-embeds unchanged content, and now also REBUILDS the knowledge
# graph for re-fetched docs when the graph extractors have been upgraded — so a graph-only
# improvement lands on your next ordinary sync (no re-embed), and a full `resync` is only
# needed to re-embed changed extraction or reach docs an incremental pull no longer re-fetches.
# `regraph` is the third option: when the extractors changed but the DOCUMENTS did not, it
# rebuilds the edges alone, without contacting the connector at all.
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
- **Streaming chat** with full-width answers — citations are inline superscripts that are
  **clickable through to the page they came from** (hover to see the source and where it goes);
  each answer has a hover toolbar to **download it as markdown**, **view all cited sources**,
  and 👍/👎 — a 👎 asks what was wrong and **learns the correction into memory**. Refusals are shown
  as "not learned yet". Answers render full markdown (tables, task/nested lists, blockquotes, code,
  links, and live mermaid diagrams).
- **Cited sources are links** — the sources panel lists each source by its readable title, linked
  to the original page, with the excerpt the answer drew on and the underlying URL. This covers
  wiki pages, work items and pipelines (TFS/Azure DevOps), merge requests, issues and CI jobs
  (GitLab/GitHub) — found live *or* from learned memory, which cite identically — and **files in
  a cloned repository**, which link straight to the file on your GitLab/GitHub/Bitbucket/Azure
  DevOps server, Octopus projects and deployments, and scraped web pages. A citation is only
  linked when its label matches a document actually retrieved this turn: if the model paraphrased
  the title, or cited something that isn't a page we hold, it stays plain text rather than sending
  you somewhere the answer was never about. Where several different pages share one title — some
  sites serve the same `<title>` for hundreds of pages — the citation isn't linked inline either;
  the sources panel lists every candidate instead of guessing one. Sources with no web address —
  a local file, a distilled conversation — are listed but not linked, rather than offered as a
  click that goes nowhere.
- **Reasoning trace** — while an answer is being worked out, the panel above it shows the run
  as a **step-by-step timeline** rather than a wall of text: the model's own reasoning and each
  tool call, interleaved in the order they actually happened. Every step names what it did
  ("Searching learned memory", "Reading the knowledge graph") and shows what it did it *with*
  (the query, the URL, the relation), and expands to reveal the full arguments and a preview of
  what came back — so you can see which thought led to which search and what that search
  returned. A step that failed is marked. The panel follows along live and collapses once the
  answer lands.
- **Download a conversation** — the **Download conversation** button at the top of the chat
  pane saves the whole exchange as one markdown file: every question and answer, its cited
  sources, and that same reasoning timeline — numbered steps with each tool's arguments and
  result — in a collapsible section. Note that reasoning traces are only captured for turns you
  watched happen in that browser tab — reloading the page or reopening an older conversation
  exports the questions and answers without them.
- **Projects and resumable conversations**, with automatic history compression.
- **Knowledge gaps** — a badge in the rail opens the knowledge-debt backlog (below).
- **Waypoints** — the interactive knowledge graph view. Drag to pan (throw the pointer and it
  coasts to a stop), scroll or pinch to zoom toward the cursor, drag any node and the layout
  makes room for it live. Nodes stay a readable size at every zoom level, and only the part of
  the graph you are looking at is drawn — on a large graph the toolbar tells you how many
  entities are in view rather than quietly showing you a fraction. Double-click a node to pull
  in its neighbours, click one to open its evidence panel, or use **Path finder** to ask how two
  entities are related.
- **⚙ Settings** — sign-in, roles & access (below), per-user connector sharing, add/test/sync/clean-up
  connectors, and tune workspace settings: LLM provider/model (incl. the
  **LiteLLM proxy URL + API-key env var**, with a **Test connection** button that pings the
  provider before you save), retrieval knobs (**hybrid** dense+sparse, **cross-encoder reranker**,
  **graph-expansion**, **contextual chunking**), **knowledge-graph** LLM triple extraction, chat
  compression, and embeddings. Toggles are labelled query-time (take effect immediately) vs
  ingest-time (need a re-sync to re-embed) — either way they apply to the running server the
  moment you save, no restart. Settings are grouped into **collapsible sections**
  that start closed, and each field tells you whether it still holds the **shipped default** —
  changed fields show what the default was, and a section header counts how many you've changed,
  so "what have I actually tuned here?" is answerable at a glance. Everything — config and
  connectors — lives in the workspace's SQLite `catalog.db`, not a YAML file. Only the fields
  you actually changed are stored, so a field left at its default keeps following QuickJoiner's
  shipped default and **picks up an improved one when you upgrade** (a retuned grounding
  threshold, say) instead of being frozen at whatever it was when the workspace was created. The
  flip side: a value you deliberately set to exactly today's default is indistinguishable from
  an untouched one, so it will move with a future retune — set it to something else, or re-set
  it after upgrading, if you want it pinned. When an upgrade does adopt a new default for you,
  the server logs the field and both values at startup, so nothing changes silently.
- Composer commands: **`/qj <plain language>`** — control QuickJoiner through its own API in
  words (connect a source, sync, teach a fact, tune settings, manage access); and `/scrape <url>`
  (crawl → a cited report with mermaid diagrams you can then choose to learn). `/qj` replaces the
  old `/connect` and `/learn` commands — just say what you want (e.g. `/qj connect a git repo for
  https://…`, `/qj teach: deploys happen on Fridays`, `/qj make the handbook connector sync
  hourly`). It asks what it needs (a git connector's URL, etc.), confirms before any change, and
  can only do what your **role** allows.

**Roles & access (RBAC).** A workspace is open (everyone is effectively an admin) until you
create the first user — who is always an **admin**. From then on, every user has a role:

| Role | Can |
|------|-----|
| **viewer** | read and ask questions only |
| **editor** | + connect sources, sync, teach facts, resolve gaps, generate briefs, scrape |
| **admin** | + change settings, reset memory, and manage users/roles |

Roles gate **both** the `/qj` chat control and the HTTP API (a viewer's write returns `403`;
destructive actions like memory reset need an explicit confirmation on top). Admins manage people
in **Settings → Account → People & access** (`GET/PATCH /api/auth/users`), or from chat
(`/qj make alice an editor`). QuickJoiner also ships a permanent **control connector** plate — it
holds no data and can't be deleted; it's just the surface for `/qj`.

The prebuilt UI is served automatically. To develop it:
`cd frontend && npm install && npm run dev` (proxies to `qj serve` on :8787); `npm run build`
emits `frontend/dist`, which `qj serve` picks up.

**Webhooks & scheduled syncs:** `POST /hooks/<source>` receivers verify GitHub
(`X-Hub-Signature-256`), GitLab (`X-Gitlab-Token`), or generic (`X-QJ-Signature`) HMAC signatures
against the source's `webhook_secret`; sources with a sync interval are refreshed by a background
scheduler.

---

## Who can read what (knowledge scopes)

QuickJoiner starts in **open mode** — no accounts, everything shared. It stays that way until
you add the first user (`qj users add`, or the first user created in the UI, who becomes the
admin). Nothing below changes anything for a single-user workspace.

Once auth is on, **each connector and each taught note is either commons or owned**:

| What it is | Who can search, cite, and see its names |
| --- | --- |
| A connector with no owner | everyone (the commons) |
| A connector you created with `--share` | everyone |
| A connector you created **privately** (the signed-in default) | only you |
| A fact you taught with `qj learn "…"` | only you |
| A fact you taught with `qj learn --share "…"` | everyone |

"Only you" means it genuinely: another user's questions never retrieve those documents, the
knowledge graph won't route an answer through relationships that rest on them, their entity
names are not offered in autocomplete or the graph search box, they don't inflate any
confidence count, and the graph's "sample of N" is counted over that person's own view. A
question that could only be answered from a source you can't read gets an honest **"I haven't
learned that yet"** — never a partial answer built from something you're not allowed to see.

Sharing is opt-in for a reason: a correction typed in the moment is one person's view, and a
wrong one taught to the commons becomes citable org truth for everyone. In chat, the
thumbs-down "help QuickJoiner learn" box has a **Share with everyone** checkbox (off by
default), and the agent's `remember` tool only shares when you say so.

⚠ Being unreadable is not yet the same as being invisible to the *ingest* side: a private
document can still influence which entities exist in the graph and how they resolve, even
though nobody else can read, cite, or enumerate it. The ingest-time merge guard and the
promotion flow (personal → org by review) are the remaining pieces — see
[docs/CLOUD_ROADMAP.md](docs/CLOUD_ROADMAP.md) Y1.8.

## Skills — teaching it *how*, not just *what*

Connectors teach QuickJoiner what your org knows. **Skills** teach it how your org works: the
query syntax for your log index, the fields your team's tickets actually use, the runbook for a
release. A skill is a folder holding `SKILL.md` — YAML frontmatter plus markdown — optionally
with `references/` for detail and `scripts/` for things it can run.

It is the **open Agent Skills format** that Claude Code and GitHub Copilot both read, so a skill
you already wrote for either works here unmodified — and one written here stays usable there.
QuickJoiner reads three folders: `<workspace>/skills/` (the portable one, where uploads land)
plus your own `~/.claude/skills/` and `~/.copilot/skills/` if they exist.

Only each skill's **name and description** ride in the prompt. The agent opens the full
instructions when it decides one applies, and follows them to a bundled reference or script from
there — so a large library costs almost nothing until it's used.

### Open skills vs. per-user ones

A skill that only explains something needs no credentials, and anyone can use it. A skill that
*reaches* a real system does — and in a shared workspace those aren't one shared login:

| The skill's scripts read | Scope | Who can run it |
| --- | --- | --- |
| nothing | **open** | everyone, no setup |
| `DEPLOY_TOKEN`, `TFS_PAT`, … | **user** | only someone who supplied their own values |

QuickJoiner decides this on first sight, from what the scripts actually read (or from an explicit
`requires_env:` in the frontmatter). **A user-scoped skill never falls back to the server's own
environment** — a credential the server holds is not yours, and running with it would quietly act
as somebody else. If you haven't supplied a value, the agent says exactly which one is missing
rather than failing vaguely.

Your values are stored **encrypted at rest** (the key lives outside the database, so a copied
`catalog.db` yields nothing on its own) and are **write-only**: no screen, endpoint, CLI command
or log line reads one back out. An admin can set workspace-wide defaults — a shared endpoint URL,
a tenant id — that everyone inherits and anyone can override with their own.

### Using them

In the UI: **Settings → Skills** lists the library, marks each one ready or *needs N*, and gives
you a box per missing value. Admins additionally install, disable, re-scope and remove.

```
qj skills list                       # what exists, and whether it's ready for you
qj skills show <name>                # the instructions the agent gets
qj skills install <folder-or-zip>    # add one to this workspace
qj skills secrets                    # which values you've set, which are still missing
qj skills set-secret DEPLOY_TOKEN    # prompts without echo (preferred over --value)
qj skills remove <name>
```

**Installing is admin-only, using is not.** A skill can bundle scripts, and a script runs with the
server's own privileges — this is not a sandbox. Install what you would be willing to run
yourself. Adding your own credentials, by contrast, is open to every user, because it is what
makes a scoped skill work *as you*.

API: `GET /api/skills`, `POST /api/skills` (multipart `.zip`), `PATCH /api/skills/{name}`,
`DELETE /api/skills/{name}`, and `GET`/`POST`/`DELETE /api/skills/secrets…`.

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
  edge validated against a fixed vocabulary **and against per-relation domain/range signatures** —
  "prod owns bob" is dropped even though every word is legal — and cited to its document).
- **Tables**, deterministically. A table is the most structured thing on a page and the hardest
  for an LLM to read: flattened to text, a member roster is a wall of unlabelled values with no
  sentence linking anyone to anything. Tables are now preserved as markdown rows at extraction —
  keeping the header, the column alignment and **empty cells** (a blank cell used to vanish and
  shift every later value in that row into the wrong column) — and mined structurally: a typed
  column (`Team`, `Repository`, `Environment`, …) plus the entity a catalogue URL names itself by
  (`/TeamDetails?team=Payments`) become edges, and a person row's **email becomes an alias**, so
  the same human known as `a.lee@corp.com` in one system and "Ann Lee" in another is one node.
  Works on any source whose text carries a table — scraped pages, Confluence, markdown, Office.
- **Graph-expansion retrieval** (on by default): once an answer is grounded, QuickJoiner walks one
  hop out in the graph to surface linked evidence the vector search missed (a ticket → the repo that
  references it → the deploy that shipped it). It never changes the grounded-vs-refuse decision.
- Surfaced as the **Waypoints** graph view, agent tools `graph_neighbors` / `graph_relations` /
  `graph_path`, and `GET /api/graph` / `GET /api/graph/path`.

**"List all X with their Y" is a graph question, not a search one.** Ask for every team with its
members, or which team owns which repo, and vector search cannot answer it — it returns the top-k
most *similar* chunks, so one overview page listing team names and head-counts fills the window
while the pages holding the actual detail never surface. The `graph_relations` tool reads one
relation across the whole graph instead, complete within its limit and citable per group, and says
so plainly when the list is truncated rather than presenting a sample as the whole answer.

When relationship extraction is on, it runs *after* the fast ingest loop — so a run that is stopped
(or a connector that pulls a **moving window**, like Azure DevOps' recent sprints) can leave
documents indexed and citable but with no relationships mined, and nothing re-provides them for a
later sync to retry. Settings → **Knowledge graph** shows that queue whenever it isn't empty, with
one button to finish it; `qj drain-graph [source]` and `POST /api/graph/drain` do the same from the
CLI/API. It re-reads the text already indexed for those documents — no connector round-trip — and
tells you plainly which ones it could rebuild in full versus only re-mine from stored text.

**Rebuilding the graph without re-syncing.** When the graph extractors improve but your documents
haven't changed, you don't need to pull everything down again: `qj regraph [source]`,
`POST /api/graph/rebuild`, the **Rebuild graph** button on any connector plate, or the workspace-wide
one in Settings → Knowledge graph all re-run the extractors over the text already indexed — no
re-fetch, no re-chunking, no re-embedding. It is deterministic and needs no LLM by default;
`--with-triples` also re-mines relationships, which is one model call per document (hours on a large
corpus), so it is opt-in and asks first.

One honest limit, shown before the job starts and again in its log: a connector's *own* structural
claims — Azure DevOps dev-links and work-item hierarchy, GitLab merge-request joins, Jira issue
links, Octopus deployments — are worked out while **fetching**, so a rebuild that skips the fetch
cannot re-derive them. Documents ingested before QuickJoiner started saving those claims therefore
keep their existing edges rather than having them rebuilt: a rebuild can add and correct there, but
not remove. Sync that source once (no re-embedding — it only backfills what the connector asserts)
and its documents become fully rebuildable.

Retrieval quality is tuned for this too: **contextual chunking** prepends each chunk with its
`source · title · path` breadcrumb (and markdown sub-chunks keep their section heading) so a
chunk's embedding carries the context it was split away from, and a **cross-encoder reranker** runs
by default over the fused candidates (`retrieval.reranker`; `QJ_DISABLE_RERANKER=1` to turn it off).
**Alias query expansion** (`retrieval.alias_expansion`, on) closes the last mile on naming: it reads
the same knowledge graph the ingest pipeline builds and, before searching, appends the formal name
for any org spoken-form in your question — so "how is connector monitor built" also finds docs
indexed only under `AppRiver.Connector.Monitor`, with no need to know the exact package name. And
`qj eval --calibrate` recommends the grounding threshold for *your* corpus from a real eval set,
so `retrieval.min_score` can be measured rather than guessed.

Speed is measured the same way, by **`qj bench`**: it breaks a query's latency down by stage —
embed, dense leg, sparse leg, fusion, reranking, the grounding gate, plus alias and graph
expansion — so you can see *where* the time goes rather than only how long it took, and
`--compare` fails a run that got more than 20% slower. Quality knobs cost real time, and this is
how you decide whether they're worth it on your corpus: on a 56k-chunk workspace the cross-encoder
reranker alone is ~77ms per candidate, which at the default depth of 24 is most of a query.
`--agent` adds end-to-end answer latency and tokens per answer, including how much of the prompt
your provider served from cache. It also reports **ingest throughput** (documents per minute,
overall and per connector) — read from the syncs you have actually run rather than by running
one, since a benchmark sync would mostly measure your network on the day. That means it needs
real runs in the window to report anything, and says so plainly when there are none.

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
qj eval my-evals.yaml --calibrate      # recommend the best retrieval.min_score for this set
                                       #   (--apply to save it; picks the max-margin midpoint,
                                       #   warns if the eval set is too thin to trust)
qj eval my-evals.yaml --compare old.json  # delta table vs a past report; non-zero exit on a
                                          #   >2-point regression (use it as a CI merge gate)
qj eval docs/evals/multi-hop-crosssource.yaml   # shipped multi-hop / cross-source eval set
qj bench my-bench.yaml --init          # write a starter bench pack (an eval set works too)
qj bench my-bench.yaml [--agent]       # SPEED + COST: where each query's milliseconds go
                                       #   (embed / dense / sparse / fuse / rerank / gate,
                                       #   p50+p95), embedder chunks/sec, and ingest docs/min
                                       #   read from your real sync history (--sync-days N);
                                       #   --agent adds time-to-first-token, answer time and
                                       #   tokens per answer (incl. prompt-cache savings)
qj bench my-bench.yaml --compare old.json  # before/after table; non-zero exit if anything got
                                           #   >20% slower or more expensive (CI merge gate)
qj extract report.pptx                 # what can QuickJoiner actually read from this file?
                                       #   per-slide character counts; --full for all text.
                                       #   Needs no workspace and ingests nothing.
qj browser login https://sso.acme.com  # persistent Playwright profile (install: pip install -e ".[browser]")
                                       #   --insecure for an internal/private-CA certificate
qj browser status https://sso.acme.com # is that saved sign-in still working?
```

### Ingesting a site behind a login

**From the web UI:** create the connector with **"Always use signed-in browser session"**
ticked, then press **Sign in to this site** on its plate. What happens next depends on where
QuickJoiner is running:

- **A machine with a display** (your desktop): a real browser window opens on it, you sign in
  once, and the plate switches from `sign-in required` to `signed in`.
- **A headless host — Docker/cloud** (no display at all): instead of a window, a **live remote
  browser view** opens right in the UI tab — QuickJoiner runs Chromium headless and streams it
  to you, so you click/type on the streamed page exactly as if it were local. Typing, paste
  (Ctrl/Cmd+V pastes *your* clipboard, not the server's), Enter/Tab/Backspace/Delete and
  select-all all work; click the view first so it has keyboard focus. Press **"Done — capture
  session"** when you're signed in (there's no window to close, so this is the explicit signal),
  or **"Run in background"** to detach — the button on the connector plate turns into **"Open
  sign-in view"** so you can come back to it. No extra setup needed — this works out of the box
  with the same `WITH_BROWSER=1` image described above, no Xvfb/VNC required.

If a later sync hits an expired session, the sync log says so and offers the same button.

**From the CLI**, point a `web_scrape` connector at it with **`use_browser=true`** and sign in once:

```powershell
qj connect web_scrape --name Plumber -o start_urls=https://plumber.acme.corp/ `
                                     -o use_browser=true -o max_pages=300
qj browser login https://plumber.acme.corp/   # real window; sign in, then close it
qj sync Plumber
```

`login` tells you *"✓ signed in — you can close the window now"* the moment the sign-in
round-trip lands, and re-checks it after the window closes rather than assuming. Two things
worth knowing, both learned the hard way:

- **Session cookies.** Most SSO sign-ins (anything `IsPersistent=false`, including ASP.NET
  Core's default) issue a cookie the browser keeps only in memory, so a browser profile alone
  loses your login the instant the window closes. QuickJoiner captures those explicitly while
  the window is open and replays them on each crawl — this is why signing in actually sticks.
- **A login page is not an error.** A gated site answers an unauthenticated request with
  `200 OK` and a sign-in form, so nothing "fails". If your session lapses, the sync says
  *"⚠ N page(s) returned a sign-in page"* and `qj test <name>` refuses rather than reporting
  a healthy connector that quietly ingests nothing.

If your SSO offers a "Use Domain Credentials" / Windows-integrated button, prefer the
username+password form — this Chromium has no enterprise auth allowlist, so the integrated
path usually dead-ends.

- **Internal/corporate CA certificates.** On your own desktop these usually just work, because
  Chromium reads the OS trust store — which your machine has already been set up with. **A
  container has not**, so an internal site signed by a private CA fails every navigation there
  with `ERR_CERT_AUTHORITY_INVALID`; the remote sign-in view then shows Chrome's certificate
  warning page instead of your login page. Turn **"Verify TLS certificate"** off on the connector
  (`-o verify_tls=false` from the CLI, `--insecure` on `qj browser login`) for internal hosts you
  trust. It's per-connector and off-by-default on purpose — QuickJoiner won't silently stop
  verifying certificates for every site it touches.

### What the crawler will and won't do

- **It stays on the site.** Links to other hosts are never followed, whatever the prefix list
  allows — an internal app links out to the trackers and repos it references, and those are
  other systems. Turn off with `-o same_host_only=false` if a site genuinely spans two hosts.
- **It visits each page once.** URLs are canonicalized first (host case, default port, duplicate
  slashes, parameter order, `utm_*` tracking parameters), so the same page under several
  spellings isn't ingested several times. Query *values* are kept exactly — `?team=30` and
  `?team=41` are different pages, and full URLs including the query string are what the
  document browser shows you.
- **It drops byte-identical repeats.** A dashboard's filter facets often render the same empty
  list under a dozen URLs; only the first is kept. The test is exact equality — pages that
  merely resemble each other are all kept.
- **It tells you what it left out.** The sync log reports duplicates skipped, and warns when it
  stopped at the page limit with links still queued (rather than looking like a complete crawl).

### Teaching it what a page means

Sites like an internal service catalogue are a goldmine — which team owns which repository, who
is on which team — but a page states that in a table whose meaning only your organization knows.
Every connector has an optional **"What these documents contain"** box:

> Each page describes one repository. The Team field is the team that owns it; Dependencies
> lists the services it calls.

That guides knowledge-graph extraction for this source: the extractor is told what a typical page
*is*, so it recognizes the relationships being stated instead of mining generic prose. It can't
loosen anything — every extracted relationship is still validated, and one the document doesn't
state is still not recorded. Needs **Extract relationships from prose** on in ⚙ Settings, and
applies to the next sync or `qj drain-graph <name>`.

The joining across pages is the knowledge graph's job, not the prompt's: "team Acadia owns
repo X" from one page and "repo X depends on Y" from another meet at the same entity, so a
question about the services behind Z is answered from pages nothing ever read together.

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
            Octopus, OneDrive/SharePoint, Grafana, Datadog, Dynatrace, Elastic,
            web_scrape)
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
