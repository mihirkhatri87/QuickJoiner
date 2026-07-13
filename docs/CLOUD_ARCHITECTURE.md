# QuickJoiner — Cloud Architecture & Implementation Plan

> Status: **proposal / future work.** The app runs today as a single container (on-prem mode).
> This document is the plan to add a **cloud mode** that runs on AWS with shared databases across
> containers, serving 50–100 developers in one org concurrently — without breaking the on-prem path.

---

## 1. Goals

| # | Goal | Why |
|---|------|-----|
| 1 | **Two modes from one codebase** | On-prem = single container, zero external deps. Cloud = horizontally scaled on AWS with shared state. |
| 2 | **Switchable persistence** | SQLite + LanceDB (files) on-prem → PostgreSQL + pgvector (shared) on cloud, chosen at boot. |
| 3 | **50–100 concurrent developers** | One shared org knowledge base; per-user + shared connectors (already built). |
| 4 | **No per-user clone sprawl** | Git working trees are transient, not durable state — never stored per user. |
| 5 | **Scheduled sync that actually works across containers** | Replace the in-process scheduler with an external, single-fire orchestrator. |

**Non-goal:** multi-*tenant* SaaS (many orgs on one deployment). Scope is one org per deployment. The
per-user/shared-connector model covers intra-org access; multi-org would be a later, larger effort.

---

## 2. Guiding principles

1. **All durable state lives in shared stores.** App containers are stateless — any container serves
   any user's request. This makes horizontal scaling trivial (no session stickiness).
2. **Only vectors + metadata are durable.** Git working trees, rendered pages, and caches are
   disposable and reproducible from the source on the next sync.
3. **Decouple ingestion from serving.** Web containers only *read* the vector store. A separate
   worker tier does the heavy, periodic work (clone, embed, ingest).
4. **The two seams already exist.** `Catalog` (metadata: config, sources, users, sessions) and
   `KnowledgeStore` (vectors) are the swap points. Formalize each as an interface; pick the backend
   from an env var. Everything else follows.

---

## 3. The persistence abstraction (the keystone)

```
                        on-prem mode                 cloud mode (AWS)
  Catalog        →  SqliteCatalog (file)      →  PostgresCatalog (Aurora)
  KnowledgeStore →  LanceStore   (file)       →  PgVectorStore  (Aurora + pgvector)
  Secrets        →  env / .env                →  AWS Secrets Manager
  SyncOrchestrator→ in-process APScheduler    →  EventBridge + SQS + worker
  LLM provider   →  anthropic / ollama         →  + bedrock (Claude on AWS)
```

- **Selection:** presence of `DATABASE_URL` (a `postgres://…` DSN) ⇒ cloud backends; otherwise the
  current SQLite/LanceDB files. One switch, no code forks in callers.
- **Interfaces:** extract the method surface `Catalog` and `KnowledgeStore` already expose into
  `Protocol`s (Python `typing.Protocol`), then implement the Postgres/pgvector versions to match.
  Call sites (`AppContext`, CLI, API) don't change — they already depend on the instances, not the
  concrete classes.
- **Recommended cloud datastore: one Aurora PostgreSQL (Serverless v2) with the `pgvector`
  extension for both catalog *and* vectors.** Rationale:
  - SQLite can't be shared across containers (single-writer, file-based) — Postgres is the natural
    catalog target.
  - LanceDB *can* sit on S3/EFS, but multi-writer concurrency across containers is its weak spot;
    pgvector gives transactional concurrency for free and collapses ops to **one** managed DB.
  - At this scale the corpus is ~10⁴–10⁵ chunks — well within pgvector (HNSW index) range. If it
    ever outgrows that, the `KnowledgeStore` interface lets you drop in Qdrant with no app changes.

---

## 4. AWS architecture

```mermaid
flowchart TB
    devs["50-100 developers"]
    idp["Org IdP (OIDC / SAML)"]
    git["GitHub / GitLab / Jira / Confluence / ..."]
    bedrock["Amazon Bedrock (Claude)"]

    subgraph AWS["AWS Account - VPC"]
        direction TB
        subgraph pub["Public subnets"]
            alb["Application Load Balancer - HTTPS via ACM"]
            nat["NAT Gateway"]
        end
        subgraph appsub["Private subnets - app tier"]
            web["ECS Fargate: web service - stateless, autoscale 2..N"]
            worker["ECS Fargate: worker service - clone + embed + ingest + sync"]
        end
        subgraph datasub["Private subnets - data tier"]
            aurora[("Aurora PostgreSQL - catalog + pgvector")]
        end
        eb["EventBridge Scheduler"]
        sqs["SQS - sync jobs"]
        secrets["Secrets Manager - connector tokens + LLM creds"]
        s3["S3 - exports / briefs / bundle cache"]
        cw["CloudWatch - logs + metrics"]
    end

    devs -->|HTTPS| alb
    alb -->|auth| idp
    alb --> web
    web --> aurora
    web --> bedrock
    web -->|Sync now| sqs
    eb -->|cron per source| sqs
    sqs --> worker
    worker --> aurora
    worker --> secrets
    worker -->|clone / pull / API| nat
    nat --> git
    worker --> bedrock
    worker --> s3
    web --> cw
    worker --> cw
```

On-prem stays a single container (no change):

```mermaid
flowchart LR
    dev["Developer"] --> app["QuickJoiner container - web + in-process scheduler"]
    app --> sqlite[("SQLite catalog")]
    app --> lance[("LanceDB vectors")]
    app --> repos["/data/repos - local git clones"]
    app --> llm["Anthropic API or local Ollama"]
```

**Component notes**
- **ALB → web (Fargate):** stateless FastAPI. Query QPS for 100 devs is low and the LLM call
  dominates latency, so 2–4 tasks with CPU-based autoscaling is plenty. No sticky sessions.
- **worker (Fargate):** ingestion/sync only — more CPU/memory for embeddings + git, scaled
  independently so heavy syncs never starve query latency.
- **Aurora + pgvector:** catalog tables (config in `settings`, `sources`, `users`, `auth_tokens`,
  `projects`, `chat_sessions`, `documents`) + a `chunks` table with a `vector` column and an HNSW
  index. Multi-AZ, private subnets.
- **EventBridge + SQS:** the replacement scheduler (see §6).
- **Secrets Manager:** connector tokens + LLM creds, never raw in the DB.
- **S3:** exports/briefs, and an optional per-source `git bundle` cache.
- **Bedrock:** Claude in-AWS with IAM auth (no API key to manage).

---

## 5. The git-clone / ingestion strategy (the crux)

**Do not persist or share clones across containers.** The git working tree is not durable state —
only the extracted vectors are. That reframing removes the problem:

1. **web never clones.** It only queries pgvector. All cloning happens on the worker.
2. **Ephemeral clone-per-sync.** For each configured `git` source the worker shallow-clones into
   task-local ephemeral storage (Fargate provides 20–200 GB), reads files + `git log`, ingests into
   pgvector (sha256-dedupe → chunk → embed → upsert), then **discards the working tree**.
3. **Incremental via catalog state, not a warm clone.** Freshness comes from re-cloning (or fetching
   the delta since the last-synced commit recorded in `sync_state`) on schedule. The existing
   doc-hash dedupe makes re-ingesting an unchanged repo cheap — hashes match, nothing re-embeds.
4. **"Different users' clones" = different source rows + per-source credentials.** Connectors are
   owned by users but are just rows in the shared `sources` table. The worker iterates them,
   resolves each source's token from Secrets Manager, clones, and ingests into the one shared org
   memory. **There is no per-user persistent clone directory — that is the point.**
5. **Large monorepo optimization (optional):** store a `git bundle` per source in S3; clone from the
   bundle and `fetch` only the delta. Speeds re-sync; not required to start.

---

## 6. Sync orchestration

The current in-process `APScheduler` breaks with N containers (each runs it → duplicate/racing
syncs). Replace it behind a `SyncOrchestrator` interface:

- **Cloud:** **EventBridge Scheduler** fires per each source's `sync_interval_minutes` → enqueues a
  job to **SQS**. The UI's "Sync now" enqueues the same job. The **worker** consumes SQS; a
  **Postgres advisory lock per source** guarantees exactly-one worker syncs a given source at a
  time. Idempotent ingestion (sha256) makes any accidental double-processing harmless.
- **On-prem:** the existing in-process scheduler is simply the other implementation of the same
  interface.

> This also fixes today's limitation where a schedule change only takes effect on server restart —
> in cloud mode the EventBridge rule is updated when the source's interval changes.

---

## 7. Secrets

- Extend `connectors/util.resolve_secret` with a `secret:<arn-or-name>` scheme alongside the
  existing `env:VAR`. Cloud resolves via Secrets Manager; on-prem keeps `env:`.
- Connector tokens and the LLM credential live in Secrets Manager; the DB stores only a reference.
- IAM: worker task role gets `secretsmanager:GetSecretValue` scoped to `quickjoiner/*`.

---

## 8. LLM on AWS

Add a **Bedrock provider** to `llm/` beside `anthropic` and `ollama` (the abstraction already
exists; see `llm/base.py` neutral message format). Claude on Amazon Bedrock keeps inference in-AWS
with IAM auth — no API key to rotate. Provider is selected by the same `llm.provider` setting,
now tunable from the UI. Prompt caching and streaming carry over.

---

## 9. Auth for an org

The built-in token model works, but for 50–100 devs wire **OIDC/SAML** (your IdP) at the ALB or in
the app, mapping the identity to the `users` table. Keep the existing per-user + shared connector
model — it already fits an org (private connectors stay with their owner; shared ones are visible to
everyone). Sessions move to Postgres, so any web container serves any user.

---

## 10. Scaling & sizing (50–100 devs)

| Concern | Assessment |
|---|---|
| Query path | Local query embedding (bge-small, ~ms) + pgvector ANN + LLM call. LLM dominates latency, not the DB. |
| Web tier | 2–4 Fargate tasks, autoscale on CPU/RPS. Stateless → linear scale. |
| Write/ingest tier | 1–few workers, scaled on SQS queue depth. Isolated from query latency. |
| Aurora | Serverless v2 auto-scales ACUs; read replicas only if query volume demands it. |
| Vector count | ~10⁴–10⁵ chunks — comfortable for pgvector HNSW. |
| Cost driver | LLM inference (Bedrock) and Aurora min-ACU floor, not compute. |

---

## 11. Observability, security, cost

- **Observability:** CloudWatch logs + metrics; alarm on SQS age (stuck syncs) and 5xx rate. Run the
  existing eval harness (`qj eval`) in CI against a staging corpus to catch grounding regressions.
- **Security:** Aurora + workers in private subnets; ALB only in public; NAT for egress (git/APIs).
  Least-privilege task roles. Secrets never in the DB or image. Optional VPC endpoints for
  Bedrock/S3/Secrets Manager to keep traffic off the public internet.
- **Cost control:** Aurora Serverless v2 scales to a low floor off-hours; Fargate scales web to a
  minimum; workers run only when the queue has jobs.

---

## 12. Implementation roadmap

Each phase is independently shippable and testable. On-prem never breaks along the way.

### Phase 0 — Groundwork (local, no AWS) — DONE (2026-07-08)
- [x] `docker-compose.cloud.yml`: app + Postgres (`pgvector/pgvector:pg16`) for local multi-container dev.
- [x] `cloud` extra in `pyproject.toml` (`psycopg[binary]`, `psycopg-pool`); Dockerfile `EXTRAS` build arg.

### Phase 1 — Persistence abstraction *(the keystone)* — DONE (2026-07-08)
- [x] `CatalogBackend` + `StoreBackend` Protocols (`memory/base.py`); shared `_SqlCatalog` base holds
      all backend-neutral SQL (portable `ON CONFLICT ... excluded`).
- [x] `PostgresCatalog` (`memory/pg_catalog.py`, psycopg pool) and `PgVectorStore`
      (`memory/pg_store.py`, pgvector `chunks` + HNSW cosine).
- [x] Factory `memory/factory.py` (`create_catalog`/`create_store`) selects on `DATABASE_URL`;
      `build_context` and `qj init` route through it. `qj init` is idempotent (provider set only on
      first init) so the cloud entrypoint can run it every boot safely.
- [x] Verified: **140 tests green** — 132 on SQLite/LanceDB, 8 on Postgres/pgvector
      (`tests/test_pg_backend.py`, gated on `QJ_TEST_DATABASE_URL`), including a **parity test** that
      ingests the same docs into both stacks and asserts identical dense retrieval ranking + scores
      (dense-only by design: the sparse legs — FTS5/porter vs Postgres/english — tokenize
      differently), a hybrid sparse-rescue + dense-gate test on each stack, plus a
      live cloud-mode `build_context` smoke (real bge-small embeddings into pgvector).
- **Run the Postgres tests:** `docker run -d -p 5433:5432 -e POSTGRES_USER=quickjoiner
  -e POSTGRES_PASSWORD=quickjoiner -e POSTGRES_DB=quickjoiner pgvector/pgvector:pg16`, then
  `QJ_TEST_DATABASE_URL=postgresql://quickjoiner:quickjoiner@localhost:5433/quickjoiner pytest -q`.
- **Deferred:** running the *entire* 116-test suite through Postgres (needs per-test schema reset);
  the parity test + backend module cover the contract for now.

### Phase 2 — Secrets provider
- [ ] `secret:<name>` scheme in `resolve_secret`; Secrets Manager client (cloud) + env (on-prem).
- **Acceptance:** a connector configured with `token=secret:…` syncs against a real source in cloud mode.

### Phase 3 — Ingestion worker + sync orchestration
- [ ] `SyncOrchestrator` interface: in-process (on-prem) vs SQS-consumer (cloud).
- [ ] `worker` entrypoint: consume SQS → ephemeral clone-per-sync → ingest → advisory-lock per source.
- [ ] EventBridge rule per source interval; UI "Sync now" and interval changes update the rule/queue.
- **Acceptance:** two workers never double-ingest one source; a schedule change takes effect without
  a restart; killing a worker mid-sync leaves no partial/dup state (idempotent).

### Phase 4 — Bedrock LLM provider
- [ ] `llm/bedrock_provider.py` implementing the neutral message format; selectable via settings.
- **Acceptance:** `qj ask` returns a grounded, cited answer via Bedrock in cloud mode.

### Phase 5 — Infrastructure as Code
- [ ] Terraform (or CDK): VPC, ALB+ACM, ECS services (web/worker), Aurora, SQS, EventBridge,
      Secrets Manager, S3, IAM, CloudWatch.
- [ ] ECS task definitions for web and worker; health checks; autoscaling policies.
- **Acceptance:** `terraform apply` stands up a working environment; UI reachable over HTTPS.

### Phase 6 — Org auth (OIDC/SAML)
- [ ] ALB OIDC or app-level OIDC mapped to the `users` table; retire password auth for cloud.
- **Acceptance:** SSO login works; connector ownership/sharing enforced by identity.

---

## 13. Open decisions

1. **pgvector vs a dedicated vector DB (Qdrant).** Start with pgvector; revisit only if scale/filtered
   search demands it. The interface makes the swap cheap.
2. **EFS for clone caching?** Default to ephemeral clone-per-sync; add S3 bundle caching only if
   large-monorepo re-clone time hurts.
3. **One shared org workspace vs per-team workspaces.** Current design assumes one shared org memory
   with per-user/shared connectors. Per-team isolation would need a `workspace_id`/tenant column.
4. **Bedrock vs direct Anthropic API from AWS.** Bedrock for IAM-native, in-AWS; direct API if a
   feature lands there first. Both supported via the provider abstraction.

---

## 14. How to view / edit the diagram

The AWS diagram above is **Mermaid**. To get it into **Lucidchart**: *Import → Mermaid* (or paste
into a Mermaid shape), which produces a fully editable Lucidchart diagram. The standalone source is
in [`aws-architecture.mmd`](./aws-architecture.mmd). It also renders as-is on GitHub, and can be
imported into diagrams.net (draw.io) the same way.
