import type {
  AuthStatus,
  BridgeEntity,
  ChatEvent,
  ConnectorRow,
  ConnectorType,
  DocumentFile,
  EntitySearchResult,
  GapsResponse,
  GraphData,
  GraphPathResult,
  ProjectRow,
  ScrapeEvent,
  SessionDetail,
  SessionRow,
  Settings,
  SourceRow,
  Status,
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
  createUser: (username: string, password: string) =>
    req<unknown>("/api/auth/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),

  learn: (fact: string, topic?: string) =>
    req<{ result: string }>("/api/learn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fact, topic: topic ?? null }),
    }),

  sources: () => req<SourceRow[]>("/api/sources"),
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
  deleteConnector: (name: string) =>
    req<unknown>(`/api/connectors/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testConnector: (name: string) =>
    req<{ ok: boolean; message: string }>(
      `/api/connectors/${encodeURIComponent(name)}/test`,
      { method: "POST" },
    ),
  syncSource: (name: string) =>
    req<{ result: string; brief?: string | null }>(`/api/sync/${encodeURIComponent(name)}`, { method: "POST" }),
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

/** Stream a chat turn. Calls onEvent for each SSE event; resolves when done. */
export function streamChat(
  body: { message: string; session_id: string | null; project: string | null },
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
