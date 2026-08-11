export interface Status {
  org: string;
  llm: { provider: string; model: string };
  stats: { sources: number; documents: number; chunks: number };
}

export type SyncState = "running" | "paused" | "retrying" | "stopping" | "stopped" | "done" | "error" | "interrupted";

export interface SyncJob {
  id: string;
  source: string;
  state: SyncState;
  /** "sync" pulls documents in; "cleanup" purges what a source taught us; "reset" wipes ALL
   * ingested memory across the workspace (source is the sentinel "all memory"); "drain"
   * mines relationships for already-ingested documents whose connector never re-yields them
   * (source is the sentinel "graph relationships"); "regraph" re-runs the graph extractors
   * over documents already ingested — no connector re-fetch, no re-chunk, no re-embed
   * (source is the sentinel "knowledge graph" when it covers every source). */
  kind: "sync" | "cleanup" | "reset" | "drain" | "regraph";
  clean: boolean;
  /** Sync jobs report ingest counts; a drain reports what it recovered. */
  stats:
    | { added: number; updated: number; skipped: number; chunks: number; errors: number }
    | { documents: number; faithful: number; text_only: number; missing_text: number; orphans: number }
    | null;
  error: string | null;
  started_at: string;
  ended_at: string | null;
  /** Current stage label (e.g. "work items · Team A"), and an estimated completion % of
   * that stage when the connector could report a total (null for open-ended streaming). */
  phase: string;
  phase_done: number | null;
  phase_total: number | null;
  percent: number | null;
  log_lines: number;
  /** Only on /api/notifications rows: true when this process still owns the running job
   * (so its live log can be streamed); false for history read back from the catalog. */
  live?: boolean;
}

export interface NotificationsResponse {
  notifications: SyncJob[];
  active: number;
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
  embedding: { provider: string; model: string | null; base_url: string; instruct: boolean };
  retrieval: {
    top_k: number;
    min_score: number;
    hybrid: boolean;
    contextual_chunks: boolean;
    reranker: string; // "fastembed" | "none"
    graph_expansion: boolean;
  };
  chat: {
    compress_after_est_tokens: number;
    keep_recent_messages: number;
    tool_result_max_chars: number;
    live_tool_result_max_chars: number;
    learn_from_conversations: boolean;
    /** IANA zone the assistant resolves "last Friday"/"this weekend" in. */
    timezone: string;
  };
  graph: { extract_triples: boolean; entity_resolution: boolean };
  repos: { auto_agents_md: boolean };
  embedding_reindex_required: boolean;
}

/** The shipped defaults (GET /api/settings/defaults), same groups as Settings minus the
 * per-workspace bits. Built server-side from fresh config models, so the drawer can mark
 * which fields are still untouched without hardcoding — and drifting from — the values. */
export type SettingDefaults = Pick<Settings, "llm" | "embedding" | "retrieval" | "chat" | "graph" | "repos">;

export interface AuthStatus {
  enabled: boolean;
  user: string | null;
  role: string; // admin | editor | viewer (admin in open mode)
}

export interface UserRow {
  username: string;
  created_at: string;
  role: string;
}

export interface SourceRow {
  id: string;
  name: string;
  type: string;
  documents: number;
  configured: boolean;
  /** Readable only by you — your own notes bucket, not a connected system. */
  private?: boolean;
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

/** An in-flight interactive browser sign-in. `mode` picks the UI: "local" opened a real
 * window on the QuickJoiner host; "remote" (headless host, e.g. Docker) streams the browser
 * back as streamed frames instead — see RemoteBrowserModal. */
export interface BrowserLoginJob {
  source: string;
  url: string;
  state: "opening" | "waiting" | "verifying" | "done" | "error";
  message: string;
  signed_in: boolean | null;
  hosts: string[];
  mode: "local" | "remote";
  started_at: string;
  ended_at: string | null;
  active: boolean;
}

/** Whether a credential-gated web_scrape connector's saved browser session still works.
 * `signed_in` comes from actually fetching the start URL — a saved profile alone proves
 * nothing, which is the trap this endpoint exists to close. Absent while a login runs. */
export interface BrowserSessionStatus {
  url: string;
  has_profile: boolean;
  hosts: string[];
  can_open_window: boolean;
  display_hint: string | null;
  /** True when this host would run a NEW sign-in as a remote (streamed) session —
   * independent of whether one is currently running, so the UI can decide up front. */
  remote_capable: boolean;
  login: BrowserLoginJob | null;
  signed_in?: boolean;
  detail?: string;
}

/** One input event for a running remote sign-in — mirrors the fixed whitelist
 * `connectors/browser/session._apply_input` accepts on the backend. */
export type RemoteBrowserInputEvent =
  | { type: "mousemove"; x: number; y: number }
  | { type: "mousedown"; x: number; y: number; button: "left" | "right" | "middle" }
  | { type: "mouseup"; button: "left" | "right" | "middle" }
  | { type: "wheel"; deltaX: number; deltaY: number }
  | { type: "keydown"; key: string }
  | { type: "keyup"; key: string }
  /** A chord such as "Control+a", sent as one press so no modifier is left stuck down. */
  | { type: "press"; key: string }
  | { type: "type"; text: string };

/** OneDrive/SharePoint sign-in state. Never carries the tokens themselves — only who
 * the connector acts as and how many documents it has been taught on demand. */
export interface OAuthStatus {
  signed_in: boolean;
  account: string;
  scopes: string[];
  expires_at?: string;
  learned: number;
}

export interface OneDriveLearnResult {
  learned: number;
  documents: { title: string; uri: string }[];
  ingested?: string;
  /** Everything that was NOT learned, and why — shown rather than swallowed. */
  notes: string[];
  warning: string;
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
  /** Editable only before the connector's first sync (0 documents) — same rule as the
   * connector name. Enforced server-side; the edit form renders it disabled after. */
  lock_after_sync?: boolean;
  /** Render as a textarea — a prose field (e.g. "what these documents contain"), not a
   * one-line value. */
  multiline?: boolean;
}
export interface ConnectorType {
  type: string;
  label: string;
  blurb: string;
  modes: string[];
  fields: ConnectorField[];
  /** What the user must do AFTER saving, for connectors that aren't usable the moment
   * they're created (OneDrive needs an interactive Microsoft 365 sign-in, which can't
   * happen before the connector exists). Rendered on the create form and echoed as the
   * post-create flash. Markdown-lite: **bold** only. */
  next_step?: string;
}

/** A user-declared label over ingested content. `uri_prefix` is the granularity:
 * '' = the whole connector · a folder path = that folder AND files ingested into it later
 * · a full document uri = just that document. */
export interface DocLabel {
  source_id: string;
  uri_prefix: string;
  kind: "tag" | "aka";
  value: string;
}

export interface IngestedDoc {
  doc_id: string;
  uri: string;
  title: string;
  kind: string;
  chunks: number;
  updated_at: string | null;
  /** Labels that apply to THIS document, including ones inherited from its folder
   * or its connector — resolved server-side at read time. */
  labels: { kind: "tag" | "aka"; value: string; uri_prefix: string }[];
  /** Connector-supplied, display-only metadata — {} for every connector that doesn't set
   * it. Azure DevOps stamps work_item_type/state/team/sprint/parent_id/changed_date/
   * closed_date here (see DocumentsModal.tsx's work-item tree, the only current reader). */
  metadata: Record<string, unknown>;
}

export interface SourceDocuments {
  source_id: string;
  documents: IngestedDoc[];
  labels: DocLabel[];
  /** Only its owner can read this source — so seeing it means it is yours, and its
   * documents can be offered to the organisation. */
  private?: boolean;
}

/** One document its author has offered to the whole organisation, awaiting review. */
export interface PromotionRow {
  doc_id: string;
  title: string;
  uri: string;
  kind: string;
  source_id: string;
  source_name: string;
  author: string;
  note: string;
  requested_at: string;
}

/** An offered document with its text, so a reviewer can actually read what they decide on. */
export interface PromotionDetail extends Omit<PromotionRow, "source_name" | "requested_at"> {
  text: string;
}

export interface PromotionResult {
  doc_id: string;
  /** "requested" | "declined" | "promoted" */
  status: string;
  source_id: string;
  message: string;
}

/** What a question is restricted to. Empty in every field = all of memory. */
export interface AskScope {
  source_ids: string[];
  doc_ids: string[];
  tags: string[];
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
/** A per-question chat context file (separate from learned memory; auto-expires). */
export interface ChatAttachment {
  id: string;
  filename: string;
  content_type?: string;
  size?: number;
  // Set once the retention sweep deleted the file — the UI then shows a warning, not a link.
  // Also carries "gone" if the row itself vanished.
  deleted_at?: string | null;
  /** Why no text could be read out of this file (picture-only deck, missing parser, corrupt).
   * Surfaced immediately so "the model ignored my file" is never the user's conclusion. */
  extract_error?: string;
}

export interface SessionDetail extends SessionRow {
  messages: { role: string; content: string; attachments?: ChatAttachment[] }[];
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
  evidence: { doc_id: string; title: string | null; uri: string | null; kind: string | null };
}
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  entity?: GraphNode; // present when the snapshot is focused on one entity
  // How big the graph actually is. A whole-graph view can only ever draw a fraction of a
  // real one (400 of 109,003 edges on a live corpus), so the denominator ships with the
  // sample — otherwise the fraction reads as the entire organization.
  totals?: { edges: number; entities: number };
  truncated?: boolean;
}
export interface EntitySearchResult {
  id: string;
  name: string;
  type: string;
  degree: number;
}
export interface BridgeEntity {
  id: string;
  name: string;
  type: string;
  source_count: number;
  degree: number;
}
export interface GraphPathResult {
  a: GraphNode;
  b: GraphNode;
  path: GraphEdge[] | null; // null = no known chain within max_hops
}
export interface DocumentFile {
  path: string;
  title: string;
  text: string;
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

/** One validated multi-angle candidate answer (plan 06 §C). `confidence` is
 * server-computed from evidence shape + corroboration — null means "unscored". */
export interface CandidateItem {
  rank: number;
  summary: string;
  confidence: number | null;
  sources: string[];
}

/** A tool invocation, as carried in the `tool_call` event's JSON `data`. */
export interface ToolCallEvent {
  id: string;
  name: string;
  args: Record<string, string>;
}

/** How that invocation came back (`tool_result` JSON `data`), paired by `id`.
 * `summary` is a bounded preview; `chars` is the true length of the full output. */
export interface ToolResultEvent {
  id: string;
  name: string;
  ok: boolean;
  summary: string;
  chars: number;
}

/** A citable reference the retrieval tools reported, so the client can turn a cited
 * title into a link (`sources` event `data`, a JSON array of these). */
export interface CitedSourceEvent {
  label: string;
  /** Identity as the tools reported it — not always browsable (a cloned repo file is
   * `<clone-url>::<path>`). Shown, never linked. */
  uri: string;
  /** Where a citation should open; absent when the document has no web address. */
  link?: string;
  kind?: string;
  score?: number;
  snippet?: string;
}

/** Streaming events the /api/chat SSE emits. */
export type ChatEvent =
  | { type: "thinking"; data: string }
  | { type: "delta"; data: string }
  | { type: "tool_call"; data: string }
  | { type: "tool_result"; data: string }
  | { type: "sources"; data: string }
  | { type: "candidates"; data: string }
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

/** Per-file result of an /api/uploads ingest. */
export interface UploadRowResult {
  file: string;
  title?: string;
  ingested: boolean;
  result?: string;
  reason?: string;
}
export interface UploadResult {
  uploaded: UploadRowResult[];
  ingested: number;
}

/** One skill in the library, joined to this workspace's configuration for it.
 *
 * `ready`/`missing` are computed per CALLER: a scoped skill is still listed when its
 * credentials are absent, so the UI can say what to set rather than hiding a capability
 * the person can see elsewhere. */
export interface SkillRow {
  name: string;
  description: string;
  /** "workspace" (installed here, ours to remove) or a personal Claude/Copilot folder. */
  origin: string;
  /** "open" — anyone may run it; "user" — it runs with each person's own credentials. */
  scope: string;
  required_env: string[];
  enabled: boolean;
  ready: boolean;
  missing: string[];
  references: string[];
  scripts: string[];
  warnings: string[];
  /** What its scripts look like they read — a suggestion for the admin form, not a
   * declaration. `required_env` is what actually gates it. */
  detected_env: string[];
  installed_by: string;
  removable: boolean;
}

export interface SkillSecrets {
  /** Names this user has set. Values are never returned by any endpoint. */
  mine: string[];
  /** Names an admin set workspace-wide; a personal value of the same name wins. */
  workspace: string[];
}

export interface SkillInstallResult {
  installed: string;
  files: number;
  replaced: boolean;
  skill: SkillRow | null;
}
