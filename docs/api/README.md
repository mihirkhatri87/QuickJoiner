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
| `username`, `password` | Only used by the auth folder. Skip that folder to stay in open mode. The first user is always an **admin**. |
| `targetUser` | The user whose role the "Set user role" request changes (admin-only RBAC: admin / editor / viewer). |
| `connectorName`, `connectorType`, `connectorPath` | The source the "connect" folder creates. Defaults create a `files` connector — set `connectorPath` to a real folder, or change `connectorType`/options for git/github/jira/etc. |
| `uploadPath` | An absolute file path on the server host for the "Ingest a server file path" request (Word/PowerPoint/Excel/PDF/Markdown/…). The multipart "Upload a document" request instead points its `files` part at a local file. |
| `askQuestion`, `searchQuery`, `suggestPrefix` | The queries used in the "query" folder. |
| `attachmentId` | **Captured automatically** by "Attach a file to a question" (per-question context files, not memory) — used by the download request. Leave blank. |
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
   This folder also holds **Upload a document** (multipart) and **Ingest a server file path** — drop
   Word/PowerPoint/Excel/PDF/Markdown/… into memory via the rolling Uploads connector.
5. **4 · Query** — search, ask a grounded question (SSE), teach a fact, autocomplete, and attach a
   file to a question (per-question context — extracted to text, injected into the turn, NOT memory;
   auto-deleted after the retention window) + download it. **Learn an attachment** promotes one of
   those files into permanent memory when you actually want it kept.
6. **5 · Graph** — snapshot, entity search, path between two entities, bridges, gaps.
7. **6 · OneDrive / SharePoint** — create the connector, sign in to Microsoft 365
   (authorization code + PKCE; `qj onedrive login <name>` is the no-browser alternative),
   check who it acts as, learn specific documents **on demand**, and sign out. This connector
   never crawls — it ingests exactly the links/paths you give it.
8. **7 · Documents, labels & scoped questions** — list what a connector ingested (each
   document's `metadata` is what powers the Azure DevOps Epic/Feature/Story/Task tree in the
   document browser), see what's inside an ingested `.zip`, tag a connector/folder/document (a
   folder tag covers files ingested later), and ask a question limited to that slice. The scope
   becomes a real pre-filter on both retrieval legs.
9. **8 · Lifecycle** — clean up one connector, delete a connector, or reset all memory.
