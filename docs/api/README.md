# QuickJoiner API — runbooks

Ready-to-import collections that walk the whole system over HTTP: **health → (optional) sign in
→ connect a source → sync & watch → query memory & graph → clean up**. The requests are chained —
login captures the bearer token, so you can run the flow top to bottom.

The live, always-current reference is the server's own **Swagger UI at `/docs`** (ReDoc at
`/redoc`, raw schema at `/openapi.json`) — grouped by function with a description on every endpoint.
These collections are the *hands-on* companion to that reference.

## Files

| Tool | What to open |
|------|--------------|
| **Postman** | Import `QuickJoiner.postman_collection.json` (Collections → Import). |
| **Bruno** | Open the `bruno/` folder as a collection (Bruno → Open Collection), then pick the **Local** environment. |

## All common config lives in ONE place

You should only ever edit variables — never the individual requests.

- **Postman:** the collection's **Variables** tab.
- **Bruno:** the **Local** environment (`bruno/environments/Local.bru`).

| Variable | Meaning |
|----------|---------|
| `baseUrl` | Where QuickJoiner is serving. Default `http://localhost:8787`. |
| `username`, `password` | Only used by the auth folder. Skip that folder to stay in open mode. |
| `connectorName`, `connectorType`, `connectorPath` | The source the "connect" folder creates. Defaults create a `files` connector — set `connectorPath` to a real folder, or change `connectorType`/options for git/github/jira/etc. |
| `askQuestion`, `searchQuery`, `suggestPrefix` | The queries used in the "query" folder. |
| `entityA`, `entityB` | The two graph nodes for the path request. |
| `token`, `sessionId` | **Filled in automatically** — login captures the token; leave blank. |

## Auth is optional

The workspace is **open** (no sign-in) until the first user exists. To run without auth, skip the
auth folder entirely. To turn auth on, run the auth folder once: it creates the first user and the
login request stores the bearer token so every later request sends `Authorization: Bearer {{token}}`.

## Streaming endpoints

`POST /api/chat`, `POST /api/scrape`, and `GET /api/sync/{name}/logs` return **Server-Sent Events**
(`text/event-stream`), not JSON. Postman and Bruno show the raw stream — read `data:` lines until a
`done` event. Everything else is plain JSON.

## Typical run

1. **0 · Status** — Health, then Workspace status.
2. **1 · Auth** *(optional)* — create user + login (captures the token).
3. **2 · Connect** — list types, create the connector, test it.
4. **3 · Sync** — start the sync, then poll `GET /api/syncs` (shows stage + %), or stream the logs.
5. **4 · Query** — search, ask a grounded question (SSE), teach a fact, autocomplete.
6. **5 · Graph** — snapshot, entity search, path between two entities, bridges, gaps.
7. **6 · Lifecycle** — clean up one connector, delete a connector, or reset all memory.
