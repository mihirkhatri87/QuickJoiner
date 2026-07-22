# Plan 08 — Natural-language self-control (`/qj`) + RBAC

**Status:** in progress (backend foundation first). **Owner:** —. **Created:** 2026-07-21.

Give a user full control of QuickJoiner from chat, in plain language, through QuickJoiner's
own API — gated by a real role/permission system so this is safe at 100+ users. The API
becomes a permanent, undeletable **control connector** that is never ingested and never in the
graph, but is fully consumable from chat. `/connect` and `/learn` are retired in favour of one
broader `/qj` command.

Decisions locked with the user (2026-07-21):
1. **Tool design — generic API dispatch.** One `qj_api` tool issues `(method, path, body)`
   calls against QuickJoiner's *own* HTTP API in-process; one `qj_api_reference` tool lists the
   endpoints the caller is allowed to use. Near-zero prompt bloat, can never drift from the real
   API, and every call is a single `(method, path)` choke point for permissions.
2. **RBAC now, full.** Roles **admin / editor / viewer**, with capability scoping AND
   connector scoping (a user's reach — including a connector's live agent tools — follows the
   connectors they can access), respecting the existing shared/personal distinction.
3. **Reuse-or-extend `auth.py` — implementer's call.** Decision below: **extend** `auth.py`
   (roles live beside users; sharing/visibility helpers stay where they are).
4. **Safety posture — always confirm.** Any mutating/destructive call (POST/PATCH/DELETE, and
   the danger tier especially) is stated and explicitly confirmed in chat before it runs;
   danger-tier endpoints additionally require a typed `confirm` argument at the tool layer.

---

## 1. Model — roles, capabilities, connector scope

### 1.1 Roles (`role` column on `users`)
- **admin** — every capability, incl. `users:admin` (create users, assign roles),
  `memory:reset`, `settings:write`, `connectors:delete`.
- **editor** — create/update/sync connectors, teach memory, resolve gaps, manage own sessions,
  generate briefs, scrape. **No** `users:admin`, `memory:reset`, or `settings:write`.
  Connector-specific writes still require the connector be **manageable** by them (own/shared).
- **viewer** — read + ask only: `*:read`, `chat:use`, `search`, `graph`, `gaps:read`,
  `sessions:read` (own). No writes of any kind.

Open mode (no users yet) = everyone is effectively **admin**, exactly today's pre-auth behaviour.
The first user created is the workspace's first **admin** (mirrors "first user turns auth on").

### 1.2 Capability taxonomy (derived from the OpenAPI tag groups × verb)
One flat set, mapped from roles:

```
status:read
connectors:read  connectors:write  connectors:delete
sync:run                      # start/stop/pause/resume/cleanup a sync
memory:write                  # /api/learn
memory:reset                  # DANGER
search:read  graph:read
gaps:read  gaps:write
sessions:read  sessions:write # create project, distill, delete
settings:read  settings:write
briefs:write                  # briefs + repo agents-md
scrape:run
users:admin                   # create users, assign roles  (admin only)
chat:use                      # /api/chat, /api/suggest
```

`ROLE_CAPS: dict[str, set[str]]` in `quickjoiner/rbac.py` is the single source of truth.

### 1.3 Connector scope
Capabilities answer *what kind* of action; connector scope answers *which connector*. A
connector-specific call (`sync`, `cleanup`, `delete`, `PATCH`, that connector's live tools)
additionally requires:
- **read/use** → the source is `visible(source, user, enabled)` (unchanged helper),
- **write/delete** → `can_manage(source, user, enabled)`.

This reuses `auth.visible`/`can_manage` verbatim — the shared/personal distinction the user
asked for already lives there. The agent's live connector tools are wired only for
`visible_sources(user)` (already true) — now additionally requires `connectors:read`, so a
viewer with no connector access simply gets no live tools.

---

## 2. Backend

### 2.1 `quickjoiner/rbac.py` (new, pure + tested)
- `ROLE_CAPS`, `ROLES = ("admin","editor","viewer")`, `DEFAULT_ROLE = "viewer"`.
- `capabilities_for(role) -> set[str]`.
- `ROUTE_CAPS: list[tuple[method, path_regex, capability, scope]]` — the
  `(method, path) → capability` map, `scope ∈ {None, "connector_read", "connector_write"}`.
- `required_capability(method, path) -> (capability, scope) | None`.
- `can(role, method, path) -> bool`.
- **Lockstep test**: enumerate `create_app(...).routes`; assert every non-SSE, non-`/health`,
  non-`/` API route resolves to a `ROUTE_CAPS` entry. A new endpoint with no capability = a
  failing test (mechanically enforces the API-docs house rule for this layer).

### 2.2 Users/roles persistence (extend `auth.py` + catalog)
- `users` table gains `role TEXT NOT NULL DEFAULT 'viewer'` (in `_MIGRATION_STATEMENTS`, both
  backends). First user → `admin`.
- `Auth`: `create_user(username, password, role="viewer")`, `set_role`, `get_role(user)`,
  `list_users()`. `role_of(user) -> "admin"` when auth disabled (open mode).
- Neutral `?`-SQL only.

### 2.3 Generic dispatch — in-process, reuses the real handlers
- `create_app(workspace, ctx=None)` refactor: reuse a passed `ctx` instead of always building
  one, so the CLI can build an app around the SAME `AppContext` (one catalog connection). The
  server path is unchanged (`ctx=None` → builds as today) and stashes `ctx.app = api`.
- Acting-user injection via a `contextvars.ContextVar` `_internal_user`: `_user()` returns it
  when set (in-process only — network requests never set it, so no bypass surface).
- `agent/control.py::build_control_tools(ctx, user, role)`:
  - `qj_api(method, path, body?, confirm?)`:
    1. `required_capability(method, path)` → 403-string if unknown route.
    2. `can(role, method, path)` → permission-denied string if not (never dispatches).
    3. connector-scope check when the path names a connector.
    4. danger tier (`memory:reset`, `connectors:delete`) requires `confirm=true`, else returns
       the exact confirmation prompt for the model to relay.
    5. dispatch via `httpx` `ASGITransport` against `ctx.app` with `_internal_user=user`;
       return status + JSON (truncated to `live_tool_result_max_chars`).
  - `qj_api_reference(area?)`: endpoint catalog (method, path, summary, capability) from
    `api.openapi()` **filtered to the caller's role** — a viewer never even sees write routes.
    For `area="connectors"` it ALSO folds in the per-type **field schema** from
    `connector_catalog()` (required/secret/env/list), so "create a connector" is answerable in
    one discovery call rather than the model having to know to hit `/api/connectors/types`.
- **Conversational connector creation replaces the deterministic `/connect` wizard.** The old
  wizard walked every required field in order; `/qj` is model-driven, so robustness comes from
  two things baked in here, not from the wizard:
  1. **Prompt guidance** (prompts.py): *before creating/updating a connector, fetch its field
     schema (`GET /api/connectors/types` or `qj_api_reference("connectors")`), ask the user for
     each REQUIRED field, store every secret as `env:VAR` (never the literal), then state the
     full plan and get an explicit yes before the POST.*
  2. **Deterministic backstop**: `POST /api/connectors` already tests the connection and does
     NOT save on failure, so a model slip (missing/wrong field) fails safely with a clear
     message the agent relays and retries — a broken connector can never persist.
  Worked example — `/qj create a new git repo connector` → agent reads git's schema (`url*`,
  `token` secret→`env:GIT_TOKEN`, `branch` default `main`, `aka` list), asks for the URL (the
  only required field), confirms, then POSTs; a bad URL/token bounces off the connection test.
- Wired in `AppContext.build_agent(..., user=None, role=None)` (thread `user`+`role`; today only
  `sources` is threaded). Callers: `api/app.py` chat handler passes the resolved user + role;
  `cli.py` passes the session user + role.

### 2.4 Control connector (permanent, undeletable, non-ingested)
- `connectors/self_connector.py`: `QuickJoinerConnector(type_name="quickjoiner", modes=LIVE)`.
  `test()` → ok; `sync()` yields nothing (never ingested, never in the graph); `tools()` is
  empty (its capability is the ctx-backed control tools, not connector.tools()).
- Seeded as a singleton default source on config load if absent (`name="quickjoiner"`,
  ownerless so it's commons, `configured` but excluded from graph/ingest — its `sync()` is a
  no-op so nothing is produced). Never counted as a data source in status doc counts.
- `DELETE /api/connectors/quickjoiner` and rename → **409 "the control connector is
  permanent."** `POST /api/sync/quickjoiner*` → 400 "nothing to sync." Hidden from the
  add-connector type list (not user-creatable).

### 2.5 New endpoints (users/roles admin — `users:admin`)
- `GET /api/auth/users` — list users + roles.
- `PATCH /api/auth/users/{username}` — set role.
- `POST /api/auth/users` already exists; extend to accept `role` (admin-only when auth on;
  first-user path still opens auth as admin).
- These get `tags`, `summary`, Postman+Bruno runbook entries, README/CLAUDE (API-docs house
  rule). `_user()` gains a role check helper `_require(cap)` used by mutating endpoints so the
  HTTP API itself is RBAC-guarded (not only the chat tool) — defense in depth.

---

## 3. Frontend

- **Remove** the `/connect` and `/learn` commands and the `wizardFlow` command entry in
  `commands.ts`. Add **`/qj <natural language>`**: routes the text to `/api/chat` with the
  control tools available (a thin command that `pushUser` + hands to the agent stage). Keep
  `startConnectFlow`/`wizard.ts` ONLY if still used by the gaps CTA — otherwise re-point the
  gaps "Connect <type>" CTA to prefill `/qj connect a <type> connector`.
- Composer slash-suggestion list: drop `/connect` `/learn`, add `/qj` (with example subtext).
- Connected-systems: render the `quickjoiner` control plate as special — a lock/badge, no
  sync/clean/delete/rename controls (its capabilities are surfaced, not its data).
- **RBAC admin UI** (admin only) in `SettingsDrawer` → a **People & access** group: list users
  with a role dropdown (`PATCH /api/auth/users/{username}`), add-user with role. Hidden for
  non-admins. `api.ts`: `listUsers`, `setUserRole`, `createUser(role)`.
- Non-admin affordances: hide/disable Danger-zone reset, connector delete, settings edit for
  viewers/editors per role (the server enforces regardless; the UI just shouldn't dangle dead
  buttons). `GET /api/auth/status` returns the caller's `role` for this.

## 4. Docs (house-rule lockstep, same change as the code)
- `CLAUDE.md`: new `rbac.py` bullet; `auth.py` bullet (roles); `agent/control.py` +
  `qj_api`/`qj_api_reference`; the control connector; the frontend `/qj` swap; the new endpoints.
- `README.md`: `/qj` usage, roles overview, the control connector.
- API-docs house rule: Swagger tags/summaries for the 2 new/extended user-role endpoints;
  Postman + Bruno runbook entries; `docs/api/README.md` if the flow changes.
- `AI_ROADMAP.md`/`PRIORITIES.md`/`STATUS.md`: this plan tracked; on ship, graduate.
- `docs/CLOUD_ROADMAP.md` Y1.8 (knowledge scopes / multi-user GA) cross-reference — RBAC is a
  prerequisite layer for it; note the relationship (RBAC governs API/connector access; knowledge
  scopes govern per-user *memory* visibility — complementary, not the same).

## 5. Tests
- `tests/test_rbac.py` — `ROLE_CAPS` shape, `can()` truth table, `required_capability` patterns,
  the **route-coverage lockstep** test.
- `tests/test_control_tools.py` — `qj_api` denies below-role calls, requires `confirm` on danger,
  connector-scope denial, a happy read round-trip (FakeEmbedder, scratch app); `qj_api_reference`
  filters by role.
- `tests/test_api.py` — role CRUD, delete-control-connector→409, seed-on-load, first-user=admin,
  a viewer bearer denied a write endpoint (server-side `_require`).
- Frontend: typecheck + a Playwright pass (control plate is undeletable; `/qj` works; admin sees
  People & access, viewer doesn't; a viewer's reset button is absent).

## 6. Build order (staged; each stage self-contained + green)
1. **Foundation** — `rbac.py`, `role` column + `Auth` role methods, tests. *(no behaviour change)*
2. **Dispatch + control tools** — `create_app(ctx=)` refactor, contextvar user, `agent/control.py`,
   wire into `build_agent`, control connector + seed + delete/rename/sync guards. Tests.
3. **HTTP RBAC + role endpoints** — `_require(cap)` on mutating routes, user/role endpoints, runbooks.
4. **Frontend** — `/qj` swap, control plate, People & access admin UI, role-aware affordances; browser-verify.
5. **Docs graduation** — fold into CLAUDE/README/roadmaps; delete this plan if nothing outstanding.

## 7. Paste-in prompt (to resume/build)
```
Build plan 08 (docs/plans/08-self-control-api-and-rbac.md) for QuickJoiner. Read that plan,
CLAUDE.md, quickjoiner/auth.py, quickjoiner/app.py, quickjoiner/api/app.py, and
quickjoiner/agent/ops.py first. Implement stage by stage (§6), venv python only
(.venv\Scripts\python.exe -m pytest -q), FakeEmbedder for unit tests, never touch
~/.quickjoiner/default. Keep the docs house rules: OpenAPI tags/summaries + Postman/Bruno
runbooks + README + CLAUDE in the SAME change as each endpoint change; update
AI_ROADMAP/PRIORITIES/STATUS. Verify the frontend in Chrome via Playwright against a scratch
workspace. Report the suite count after each stage.
```

## Safety & invariants
- The refusal/grounding contract is untouched — this is a control plane, not a retrieval change.
- RBAC is enforced **twice**: in `qj_api` before dispatch (the chat path) AND by `_require` on
  the HTTP routes (the direct-API path) — a stricter check at the tool layer can't be bypassed
  by hitting the API directly.
- The control connector produces no documents, so it can never pollute memory or the graph.
- Danger-tier actions need role + connector scope + typed `confirm` + chat confirmation — four
  independent gates before anything destructive runs from natural language.
