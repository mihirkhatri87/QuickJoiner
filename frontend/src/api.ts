import type {
  AuthStatus,
  BridgeEntity,
  BrowserLoginJob,
  BrowserSessionStatus,
  ChatEvent,
  ConnectorRow,
  ConnectorType,
  DocumentFile,
  EntitySearchResult,
  GapsResponse,
  GraphData,
  GraphPathResult,
  NotificationsResponse,
  ProjectRow,
  ScrapeEvent,
  SessionDetail,
  SessionRow,
  SettingDefaults,
  Settings,
  SourceRow,
  Status,
  ChatAttachment,
  SyncJob,
  UploadResult,
  UserRow,
  AskScope,
  DocLabel,
  SourceDocuments,
  OAuthStatus,
  OneDriveLearnResult,
} from "./types";

const TOKEN_KEY = "qj_token";
const USER_KEY = "qj_user";

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string, user: string) => {
    localStorage.setItem(TOKEN_KEY, t);
    localStorage.setItem(USER_KEY, user);
  },
  clear: () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  },
};

function authHeaders(): Record<string, string> {
  const t = token.get();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const resp = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers as Record<string, string>), ...authHeaders() },
  });
  const data = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = (data && (data as { detail?: string }).detail) || `${resp.status}`;
    throw new Error(detail);
  }
  return data as T;
}

export const api = {
  status: () => req<Status>("/api/status"),
  settings: () => req<Settings>("/api/settings"),
  settingDefaults: () => req<SettingDefaults>("/api/settings/defaults"),
  updateSettings: (body: Record<string, unknown>) =>
    req<Settings>("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  testLLM: (llm: Record<string, unknown>) =>
    req<{ ok: boolean; model: string; message: string }>("/api/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm }),
    }),

  authStatus: () => req<AuthStatus>("/api/auth/status"),
  login: (username: string, password: string) =>
    req<{ token: string; username: string }>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => req<unknown>("/api/auth/logout", { method: "POST" }),
  createUser: (username: string, password: string, role?: string) =>
    req<{ username: string; role: string }>("/api/auth/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role: role ?? null }),
    }),
  // People & access (admin only). listUsers/setUserRole 403 for non-admins.
  listUsers: () => req<UserRow[]>("/api/auth/users"),
  setUserRole: (username: string, role: string) =>
    req<{ username: string; role: string }>(`/api/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    }),

  learn: (fact: string, topic?: string) =>
    req<{ result: string }>("/api/learn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fact, topic: topic ?? null }),
    }),

  // Upload documents straight into learned memory via the rolling Uploads connector. This is
  // the EXPLICIT "learn this permanently" path (used by /qj and the API) — the chat composer's
  // attach button uses uploadChatAttachments below instead, which is per-question context only.
  uploadDocuments: async (files: File[]): Promise<UploadResult> => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    const resp = await fetch("/api/uploads", { method: "POST", headers: { ...authHeaders() }, body: form });
    const data = resp.status === 204 ? null : await resp.json().catch(() => null);
    if (!resp.ok) throw new Error((data && (data as { detail?: string }).detail) || `${resp.status}`);
    return data as UploadResult;
  },

  // Attach file(s) as per-question CONTEXT for a chat message. Extracted to text and injected
  // into that turn only — NOT ingested into memory or the Uploads connector, and auto-deleted
  // after the retention window. Returns metadata to pass as attachment_ids on streamChat.
  //
  // Uses XMLHttpRequest (not fetch) specifically for `xhr.upload.onprogress` — a several-MB
  // file with nothing rendering while it sends read as a frozen UI (reported live). Reports two
  // distinct phases: "uploading" tracks real bytes sent, then "processing" once every byte has
  // left the browser but the response hasn't arrived yet — text extraction (unzipping an
  // archive, parsing a large office doc) happens server-side inside that same request, so a
  // multi-second gap AFTER 100% is expected, not a stall. onProgress is best-effort; a caller
  // that doesn't pass one gets the exact same request with no observable difference.
  uploadChatAttachments: (
    files: File[],
    onProgress?: (state: { phase: "uploading" | "processing"; pct: number }) => void,
  ): Promise<ChatAttachment[]> =>
    new Promise((resolve, reject) => {
      const form = new FormData();
      for (const f of files) form.append("files", f);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/chat/attachments");
      for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
      xhr.upload.onprogress = (e) => {
        if (!onProgress || !e.lengthComputable) return;
        onProgress({ phase: "uploading", pct: Math.round((e.loaded / e.total) * 100) });
      };
      xhr.upload.onload = () => onProgress?.({ phase: "processing", pct: 100 });
      xhr.onload = () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- matches req()'s error-body handling below
        let data: any = null;
        try {
          data = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch {
          /* non-JSON error body (e.g. a proxy error page) — status check below still fires */
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve((data as { attachments: ChatAttachment[] }).attachments);
        } else {
          reject(new Error((data && (data as { detail?: string }).detail) || `${xhr.status}`));
        }
      };
      xhr.onerror = () => reject(new Error("Network error while uploading"));
      xhr.send(form);
    }),
  // Ingest a file by PATH on the machine QuickJoiner runs on, straight into the rolling
  // Uploads connector. Deterministic twin of asking the agent to do it — the /ingest command
  // uses this so a model that declines ("I can't read your files") can't block the user.
  ingestLocalPath: (path: string) =>
    req<{ file: string; title: string; ingested: boolean; result?: string; reason?: string }>(
      "/api/uploads/local",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) },
    ),
  // Promote an already-uploaded chat attachment into permanent memory (the rolling Uploads
  // connector). Separate from uploadChatAttachments on purpose: attaching is per-question
  // context, learning is a memory mutation the user opts into.
  //
  // Embedding a large document (e.g. a whole zip's concatenated text) is real CPU work that
  // can run minutes on a machine with no GPU — reported live as the chat composer "just
  // sitting there" with the request looking hung. onProgress, when passed, gets REAL
  // (chunks embedded, chunks total) counts: this mints a token, starts polling
  // GET /api/ingest-progress/{token} in parallel with the POST, and stops polling once the
  // POST settles either way. Omit onProgress and this is byte-identical to a plain POST.
  learnChatAttachment: async (
    id: string,
    onProgress?: (state: { done: number; total: number }) => void,
  ): Promise<{ file: string; title: string; ingested: boolean; result?: string; reason?: string }> => {
    if (!onProgress) {
      return req(`/api/chat/attachments/${encodeURIComponent(id)}/learn`, { method: "POST" });
    }
    const token = crypto.randomUUID ? crypto.randomUUID() : `p${Date.now()}-${Math.random()}`;
    const poll = setInterval(async () => {
      try {
        const resp = await fetch(`/api/ingest-progress/${token}`, { headers: { ...authHeaders() } });
        if (resp.status === 200) {
          const row = await resp.json();
          onProgress({ done: row.done, total: row.total });
        }
      } catch {
        /* a missed poll just means one stale UI tick — the next one (or the final POST
           result) catches up, so failures here are silently ignored rather than surfaced */
      }
    }, 600);
    try {
      return await req(
        `/api/chat/attachments/${encodeURIComponent(id)}/learn?progress_token=${token}`,
        { method: "POST" },
      );
    } finally {
      clearInterval(poll);
    }
  },
  // Download a chat context file via an auth'd fetch → blob (a plain <a href> can't send the
  // bearer token). Throws a friendly message on 410 (the file expired and was deleted).
  downloadChatAttachment: async (id: string, filename: string): Promise<void> => {
    const resp = await fetch(`/api/chat/attachments/${encodeURIComponent(id)}/download`, {
      headers: { ...authHeaders() },
    });
    if (!resp.ok) {
      throw new Error(resp.status === 410 ? "This file has expired and was deleted." : `${resp.status}`);
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  },

  sources: () => req<SourceRow[]>("/api/sources"),
  // What a connector has actually ingested, each document with the labels that apply to
  // it (including ones inherited from its folder or the connector itself).
  sourceDocuments: (sourceId: string) =>
    req<SourceDocuments>(`/api/sources/${encodeURIComponent(sourceId)}/documents`),
  // What's inside an ingested .zip — recovered from the document's own extracted text
  // (there's no separate member-list column), for the document browser's expand-to-see-
  // contents view.
  documentArchive: (sourceId: string, docId: string) =>
    req<{ members: string[]; notes: string[] }>(
      `/api/sources/${encodeURIComponent(sourceId)}/documents/archive?doc_id=${encodeURIComponent(docId)}`,
    ),
  labels: () => req<{ labels: DocLabel[] }>("/api/labels"),
  addLabel: (body: DocLabel) =>
    req<{ ok: boolean; labels: DocLabel[] }>("/api/labels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  removeLabel: (body: DocLabel) =>
    req<{ ok: boolean; labels: DocLabel[] }>("/api/labels", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  connectorTypes: () => req<ConnectorType[]>("/api/connectors/types"),
  connectors: () => req<ConnectorRow[]>("/api/connectors"),
  createConnector: (body: Record<string, unknown>) =>
    req<ConnectorRow>("/api/connectors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  patchConnector: (name: string, body: Record<string, unknown>) =>
    req<ConnectorRow>(`/api/connectors/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // Deleting purges what the connector taught us: its source_id is `type:name`, so
  // documents left behind are unreachable — nothing could re-sync or purge them later.
  // keepMemory retires the connector but keeps its knowledge.
  deleteConnector: (name: string, keepMemory = false) =>
    req<{ removed: string; job: SyncJob | null }>(
      `/api/connectors/${encodeURIComponent(name)}${keepMemory ? "?keep_memory=true" : ""}`,
      { method: "DELETE" },
    ),
  cleanupConnector: (name: string) =>
    req<{ job: SyncJob }>(`/api/connectors/${encodeURIComponent(name)}/cleanup`, { method: "POST" }),
  testConnector: (name: string) =>
    req<{ ok: boolean; message: string }>(
      `/api/connectors/${encodeURIComponent(name)}/test`,
      { method: "POST" },
    ),
  // --- OneDrive / SharePoint: per-user Microsoft 365 sign-in + on-demand learning.
  // The status response deliberately never carries the tokens themselves, only who
  // the connector is signed in as and how much it has been taught.
  oauthStatus: (name: string) =>
    req<OAuthStatus>(`/api/connectors/${encodeURIComponent(name)}/oauth/status`),
  oauthStart: (name: string) =>
    req<{ authorize_url: string; state: string; redirect_uri: string }>(
      `/api/connectors/${encodeURIComponent(name)}/oauth/start`,
      { method: "POST" },
    ),
  // Credential-gated web_scrape sign-in. Unlike the Microsoft flow there is no redirect:
  // a real browser window opens ON THE QUICKJOINER HOST (same machine as the UI in a
  // local-first setup), so this starts a background job the UI polls.
  browserLoginStart: (name: string) =>
    req<{ login: BrowserLoginJob }>(
      `/api/connectors/${encodeURIComponent(name)}/browser/login`,
      { method: "POST" },
    ),
  // `verify=false` answers from stored cookies alone — cheap enough to poll while a
  // sign-in window is open; the default actually fetches the start URL.
  browserSession: (name: string, verify = true) =>
    req<BrowserSessionStatus>(
      `/api/connectors/${encodeURIComponent(name)}/browser/session?verify=${verify}`,
    ),
  oauthSignOut: (name: string) =>
    req<{ signed_out: boolean }>(`/api/connectors/${encodeURIComponent(name)}/oauth`, {
      method: "DELETE",
    }),
  // Ingest specific documents on demand. This connector never crawls: `targets` are
  // shared links or paths, and a folder learns the readable files beneath it.
  onedriveLearn: (name: string, targets: string[]) =>
    req<OneDriveLearnResult>(`/api/connectors/${encodeURIComponent(name)}/onedrive/learn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets }),
    }),
  // Start an async sync job (optionally a clean/purge-first resync). Different sources
  // can run at once; watch a job with streamSyncLogs and halt it with stopSync.
  startSync: (name: string, clean = false) =>
    req<{ job: SyncJob }>(`/api/sync/${encodeURIComponent(name)}${clean ? "?clean=true" : ""}`, { method: "POST" }),
  stopSync: (name: string, cleanup = false) =>
    req<{ job: SyncJob }>(`/api/sync/${encodeURIComponent(name)}/stop${cleanup ? "?cleanup=true" : ""}`, { method: "POST" }),
  // Hold / continue a running sync in place — the same in-memory run, so resume doesn't
  // re-pull. Pausing covers both the document pull and the deferred graph-extraction tail.
  pauseSync: (name: string) =>
    req<{ job: SyncJob }>(`/api/sync/${encodeURIComponent(name)}/pause`, { method: "POST" }),
  resumeSync: (name: string) =>
    req<{ job: SyncJob }>(`/api/sync/${encodeURIComponent(name)}/resume`, { method: "POST" }),
  listSyncs: () => req<{ syncs: SyncJob[] }>("/api/syncs"),
  // Wipe ALL ingested knowledge (documents, vectors, graph, watermarks); keeps connectors
  // configured. Runs as a background job (streams logs, lands in the activity feed) — the
  // returned job's source is the sentinel "all memory". Refuses (409) while any sync runs.
  resetMemory: () => req<{ job: SyncJob }>("/api/memory/reset", { method: "POST" }),
  // Documents that were ingested but whose relationship extraction never resolved — a
  // connector that ingests a moving window never re-provides them, so nothing retries it.
  graphPending: () =>
    req<{ total: number; by_source: { source_id: string; count: number }[]; extraction_enabled: boolean }>(
      "/api/graph/pending",
    ),
  // Mine those documents' relationships from the text already indexed for them. Background
  // job (sentinel source "graph relationships"); 409 while another job runs or extraction is off.
  drainGraph: (sourceId?: string) =>
    req<{ job: SyncJob }>(
      `/api/graph/drain${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`,
      { method: "POST" },
    ),
  // Sync activity over a rolling window (running + finished), newest first. Survives a
  // page reload and a server restart — the backend persists it. Backs the bell menu.
  notifications: (hours = 24) => req<NotificationsResponse>(`/api/notifications?hours=${hours}`),
  streamSyncLogs: (name: string, onEvent: (e: { type: string; line?: string; job?: SyncJob }) => void) =>
    streamGetSSE(`/api/sync/${encodeURIComponent(name)}/logs`, onEvent),

  // Convenience for simple flows (wizard / post-scrape): start a sync and resolve once
  // it finishes, returning a short result string like the old blocking endpoint did.
  syncSource: async (name: string): Promise<{ result: string; job: SyncJob }> => {
    await req(`/api/sync/${encodeURIComponent(name)}`, { method: "POST" });
    for (;;) {
      await new Promise((r) => setTimeout(r, 400));
      const { syncs } = await req<{ syncs: SyncJob[] }>("/api/syncs");
      const job = syncs.find((s) => s.source === name);
      if (job && ["done", "error", "stopped"].includes(job.state)) {
        const s = job.stats;
        const result =
          s && "added" in s
            ? `${s.added} added, ${s.updated} updated, ${s.skipped} unchanged`
            : job.error || job.state;
        return { result, job };
      }
    }
  },
  generateAgentsMd: (name: string) =>
    req<{ brief: string; path: string }>(`/api/repos/${encodeURIComponent(name)}/agents-md`, { method: "POST" }),

  graph: (entity?: string, limit = 400) =>
    req<GraphData>(
      "/api/graph?" +
        new URLSearchParams({
          ...(entity ? { entity } : {}),
          limit: String(limit),
        }).toString(),
    ),
  searchEntities: (q: string, limit = 8) =>
    req<EntitySearchResult[]>(
      "/api/graph/search?" + new URLSearchParams({ q, limit: String(limit) }).toString(),
    ),
  graphBridges: (limit = 20) =>
    req<BridgeEntity[]>("/api/graph/bridges?" + new URLSearchParams({ limit: String(limit) }).toString()),
  graphPath: (a: string, b: string, maxHops = 4) =>
    req<GraphPathResult>(
      "/api/graph/path?" +
        new URLSearchParams({ a, b, max_hops: String(maxHops) }).toString(),
    ),
  documentFile: (docId: string) =>
    req<DocumentFile>(`/api/documents/${encodeURIComponent(docId)}/file`),

  suggest: (q: string, limit = 6) =>
    req<{ suggestions: string[] }>(
      "/api/suggest?" + new URLSearchParams({ q, limit: String(limit) }).toString(),
    ),

  gaps: () => req<GapsResponse>("/api/gaps"),
  resolveGaps: (gapIds: string[], resolution: string) =>
    req<{ resolved: number }>("/api/gaps/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gap_ids: gapIds, resolution }),
    }),

  projects: () => req<ProjectRow[]>("/api/projects"),
  createProject: (name: string) =>
    req<ProjectRow>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  sessions: (project?: string) =>
    req<SessionRow[]>("/api/sessions" + (project ? `?project=${encodeURIComponent(project)}` : "")),
  session: (id: string) => req<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`),
  distill: (id: string) =>
    req<{ facts_learned: number }>(`/api/sessions/${encodeURIComponent(id)}/distill`, {
      method: "POST",
    }),
  deleteSession: (id: string) =>
    req<{ deleted: string }>(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteAllSessions: (project?: string) =>
    req<{ deleted: number }>("/api/sessions" + (project ? `?project=${encodeURIComponent(project)}` : ""), {
      method: "DELETE",
    }),
};

async function streamSSE<E>(path: string, body: unknown, onEvent: (e: E) => void): Promise<void> {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => null);
    throw new Error((data && (data as { detail?: string }).detail) || `${resp.status}`);
  }
  if (!resp.body) throw new Error("no response stream");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      if (!part.startsWith("data: ")) continue;
      onEvent(JSON.parse(part.slice(6)) as E);
    }
  }
}

/** Read a GET Server-Sent-Events stream (e.g. live sync logs) until it ends. */
async function streamGetSSE<E>(path: string, onEvent: (e: E) => void): Promise<void> {
  const resp = await fetch(path, { headers: { ...authHeaders() } });
  if (!resp.ok || !resp.body) throw new Error(`${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      if (part.startsWith("data: ")) onEvent(JSON.parse(part.slice(6)) as E);
    }
  }
}

/** Stream a chat turn. Calls onEvent for each SSE event; resolves when done. */
export function streamChat(
  body: {
    message: string;
    session_id: string | null;
    project: string | null;
    attachment_ids?: string[];
    // Narrow this question to chosen connectors/documents/tags. Omitted = all memory.
    scope?: AskScope;
  },
  onEvent: (e: ChatEvent) => void,
): Promise<void> {
  return streamSSE("/api/chat", body, onEvent);
}

/** Stream a /scrape run: crawl progress, synthesis deltas, then the report. */
export function streamScrape(
  body: { url: string; depth?: number; max_pages?: number },
  onEvent: (e: ScrapeEvent) => void,
): Promise<void> {
  return streamSSE("/api/scrape", body, onEvent);
}
