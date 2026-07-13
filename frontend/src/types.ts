export interface Status {
  org: string;
  llm: { provider: string; model: string };
  stats: { sources: number; documents: number; chunks: number };
}

export interface LLMSettings {
  provider: string;
  model: string | null;
  base_url: string;
  max_tokens: number;
  thinking: boolean;
  thinking_budget: number;
  api_key_env: string;
}
export interface Settings {
  org: string;
  llm: LLMSettings;
  embedding: { provider: string; model: string | null; base_url: string };
  retrieval: { top_k: number; min_score: number };
  chat: {
    compress_after_est_tokens: number;
    keep_recent_messages: number;
    tool_result_max_chars: number;
    learn_from_conversations: boolean;
  };
  embedding_reindex_required: boolean;
}

export interface AuthStatus {
  enabled: boolean;
  user: string | null;
}

export interface SourceRow {
  id: string;
  name: string;
  type: string;
  documents: number;
  configured: boolean;
}

export interface ConnectorRow {
  name: string;
  type: string;
  owner: string | null;
  shared: boolean;
  mine: boolean;
  can_manage: boolean;
  documents: number;
  last_sync: string | null;
  sync_interval_minutes: number | null;
  options: Record<string, unknown>;
  modes: string[];
}

export interface ConnectorField {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
  env: string | null;
  placeholder: string;
  help: string;
  list: boolean;
}
export interface ConnectorType {
  type: string;
  label: string;
  blurb: string;
  modes: string[];
  fields: ConnectorField[];
}

export interface ProjectRow {
  id: string;
  name: string;
  session_count: number;
}
export interface SessionRow {
  id: string;
  project_id: string | null;
  title: string;
  summary: string;
  est_tokens: number;
  created_at: string;
  updated_at: string;
}
export interface SessionDetail extends SessionRow {
  messages: { role: string; content: string }[];
}

export interface GraphNode {
  id: string;
  name: string;
  type: string; // repo | package | project | ticket | service | environment | source
}
export interface GraphEdge {
  src: string;
  rel: string;
  dst: string;
  detail: string;
  evidence: { doc_id: string; title: string | null; uri: string | null };
}
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  entity?: GraphNode; // present when the snapshot is focused on one entity
}

export interface GapCluster {
  id: string;
  label: string; // representative query, or "(private refusals)" in hash-only mode
  count: number;
  samples: string[];
  suggested_connectors: string[];
  entity_hints: { id: string; name: string; type: string }[];
  gap_ids: string[];
}
export interface GapsResponse {
  open_count: number;
  clusters: GapCluster[];
}

/** Streaming events the /api/chat SSE emits. */
export type ChatEvent =
  | { type: "thinking"; data: string }
  | { type: "delta"; data: string }
  | { type: "tool_call"; data: string }
  | { type: "answer"; data: string; session_id: string }
  | { type: "error"; data: string }
  | { type: "done" };

/** Streaming events the /api/scrape SSE emits. */
export type ScrapeEvent =
  | { type: "status"; data: string }
  | { type: "delta"; data: string }
  | { type: "answer"; data: string; path: string; pages: number }
  | { type: "error"; data: string }
  | { type: "done" };
