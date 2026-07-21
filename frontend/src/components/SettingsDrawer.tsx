import {
  ChevronLeft,
  ChevronRight,
  Eraser,
  FileText,
  Lock,
  Plug,
  RefreshCw,
  RotateCcw,
  Settings as Gear,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api, token } from "../api";
import type { AuthStatus, ConnectorRow, ConnectorType, SettingDefaults, Settings, SyncJob } from "../types";
import { EditConnectorModal } from "./EditConnectorModal";
import { Button, cn, Field, IconButton, schedLabel, Select, SYNC_OPTIONS, TextInput } from "./ui";

const ALL_MODES = ["pull", "hooks", "live", "browser", "scrape"];

// Fields each settings group actually renders — the "N changed" badges count only these,
// so a badge never points at a config key with no control behind it. Keep in sync when
// adding a control (a knob added here without a control would badge a section you can't fix).
const LLM_KEYS = ["provider", "model", "base_url", "api_key_env", "max_tokens", "thinking", "thinking_budget"];
const RETRIEVAL_KEYS = ["top_k", "min_score", "hybrid", "reranker", "graph_expansion", "contextual_chunks"];
const GRAPH_KEYS = ["extract_triples", "entity_resolution"];
const REPOS_KEYS = ["auto_agents_md"];
const CHAT_KEYS = [
  "compress_after_est_tokens", "keep_recent_messages",
  "tool_result_max_chars", "live_tool_result_max_chars", "learn_from_conversations",
];
const EMBEDDING_KEYS = ["provider", "model", "instruct"];

export function SettingsDrawer({
  open,
  onClose,
  onChanged,
  onOpenArtifact,
  syncJobs,
  onOpenSync,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
  onOpenArtifact?: (a: { title: string; markdown: string }) => void;
  syncJobs: SyncJob[];
  /** Opens the shared sync log viewer (owned by App, so it outlives this drawer). */
  onOpenSync: (source: string, clean: boolean, autoStart?: boolean, kind?: "sync" | "cleanup") => void;
}) {
  const [auth, setAuth] = useState<AuthStatus>({ enabled: false, user: null });
  const [status, setStatus] = useState("");
  const [statusOk, setStatusOk] = useState(true);

  const refreshAuth = async () => {
    try {
      setAuth(await api.authStatus());
    } catch {
      setAuth({ enabled: false, user: null });
    }
  };
  useEffect(() => {
    if (open) {
      setStatus("");
      refreshAuth();
    }
  }, [open]);

  const flash = (msg: string, ok = true) => {
    setStatus(msg);
    setStatusOk(ok);
  };

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-20 bg-black/55 backdrop-blur-[2px] transition-opacity",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        className={cn(
          "fixed bottom-3 right-3 top-3 z-30 flex w-[min(476px,calc(100vw-24px))] flex-col rounded-lg bg-panel shadow-panel backdrop-blur-xl transition-transform duration-200",
          open ? "translate-x-0" : "translate-x-[106%]",
        )}
      >
        <div className="flex items-center gap-3 p-5 pb-3">
          <span className="flex h-[34px] w-[34px] items-center justify-center rounded-full bg-accent-soft text-accent">
            <Gear size={16} />
          </span>
          <span className="font-display text-[16px] font-semibold tracking-tight">Settings</span>
          <button
            onClick={onClose}
            aria-label="Close settings"
            className="ml-auto flex h-9 w-9 items-center justify-center rounded-full text-muted transition hover:bg-fill2 hover:text-ink"
          >
            <X size={17} />
          </button>
        </div>

        <div className="scroll-thin flex flex-1 flex-col gap-6 overflow-y-auto p-5 pt-1">
          <div className={cn("min-h-[15px] font-mono text-[11.5px]", statusOk ? "text-accent" : "text-danger")}>
            {status}
          </div>

          <Section title="Account">
            <Account auth={auth} onFlash={flash} onChanged={() => { refreshAuth(); onChanged(); }} />
          </Section>

          <Section title="Workspace">
            <WorkspaceSettings locked={auth.enabled && !auth.user} onFlash={flash} onChanged={onChanged} />
          </Section>

          <Section title="Connectors">
            <Connectors auth={auth} onFlash={flash} onChanged={onChanged} onOpenArtifact={onOpenArtifact}
                        syncJobs={syncJobs} onOpenSync={onOpenSync} />
          </Section>

          <Section title="Danger zone">
            <DangerZone locked={auth.enabled && !auth.user} onFlash={flash} onChanged={onChanged} />
          </Section>
        </div>
      </aside>
    </>
  );
}

/** Global "reset all memory" — wipes everything ingested, keeps connectors configured.
 * Type-to-confirm, because it's irreversible and workspace-wide. */
function DangerZone({
  locked,
  onFlash,
  onChanged,
}: {
  locked: boolean;
  onFlash: (m: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = async () => {
    setBusy(true);
    try {
      const r = await api.resetMemory();
      const c = r.removed;
      onFlash(`Memory reset — removed ${c.documents} documents, ${c.edges} graph edges. Connectors kept.`, true);
      setConfirming(false);
      setTyped("");
      onChanged();
    } catch (e) {
      onFlash(String((e as Error).message), false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-danger-soft bg-danger-soft/40 p-4">
      <div className="text-[13px] font-semibold text-danger">Reset all learned memory</div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-muted">
        Deletes every ingested document, its vectors, and the whole knowledge graph, and clears each
        connector's sync watermark — the workspace goes back to as if nothing had ever synced.
        <b className="text-ink"> Your connectors stay configured</b> (re-sync any time); chat history and
        settings are untouched. This cannot be undone.
      </p>
      {!confirming ? (
        <Button
          variant="danger"
          className="mt-3"
          disabled={locked}
          onClick={() => setConfirming(true)}
        >
          Reset all memory
        </Button>
      ) : (
        <div className="mt-3">
          <label className="text-[12px] text-muted">
            Type <span className="font-mono font-semibold text-ink">RESET</span> to confirm:
          </label>
          <div className="mt-1.5 flex flex-wrap gap-2">
            <TextInput
              autoFocus
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder="RESET"
              className="max-w-[160px]"
            />
            <Button variant="danger" disabled={typed !== "RESET" || busy} onClick={reset}>
              {busy ? "Resetting…" : "Confirm reset"}
            </Button>
            <Button
              onClick={() => {
                setConfirming(false);
                setTyped("");
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-3 px-1 text-[10.5px] font-semibold uppercase tracking-[0.18em] text-faint">{title}</div>
      {children}
    </div>
  );
}

/** Labelled checkbox with an optional sub-hint — used for the retrieval/graph knobs. */
function Toggle({
  checked,
  onChange,
  label,
  hint,
  note,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
  note?: React.ReactNode;
}) {
  return (
    <label className="my-2.5 flex items-start gap-2.5 text-[12.5px]">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-[15px] w-[15px] flex-shrink-0 accent-[var(--accent)]"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="text-ink">{label}</span>
          {note && <span className="ml-auto flex-shrink-0">{note}</span>}
        </span>
        {hint && <span className="mt-0.5 block text-[11px] leading-snug text-faint">{hint}</span>}
      </span>
    </label>
  );
}

/** Collapsible settings group. Every group is closed on open — the drawer is a wall of
 * knobs otherwise — and each header carries a count of the fields in it that differ from
 * the shipped defaults, so "where did I change something?" is answerable without expanding
 * anything. */
function Group({
  title,
  changed,
  locked,
  children,
}: {
  title: string;
  changed: number;
  /** Editing is gated (signed out), but *reading* never is — the header stays clickable
   * and only the controls inside are disabled. */
  locked?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-[var(--border)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 py-3 text-left text-[11px] font-semibold uppercase tracking-[0.14em] text-accent transition hover:brightness-125"
      >
        <ChevronRight size={13} className={cn("flex-shrink-0 transition-transform", open && "rotate-90")} />
        {title}
        {changed > 0 && (
          <span className="ml-auto flex-shrink-0 rounded-full bg-gold-soft px-2 py-0.5 font-mono text-[9px] normal-case tracking-[0.06em] text-gold">
            {changed} changed
          </span>
        )}
      </button>
      {open && (
        <fieldset disabled={locked} className={cn("pb-3", locked && "opacity-60")}>
          {children}
        </fieldset>
      )}
    </div>
  );
}

const fmtDefault = (v: unknown): string =>
  typeof v === "boolean" ? (v ? "on" : "off") : v === null || v === "" ? "unset" : String(v);

/** True when a field still holds its shipped default. An empty text input and a null
 * config value are the same thing to the user ("default for provider"), so they compare
 * equal here — otherwise every optional field would read as customized. */
const isDefault = (current: unknown, def: unknown): boolean =>
  (current ?? "") === (def ?? "");

/** The per-field default marker: neutral when the field is untouched, gold with the
 * original value when it has been changed — so the drawer answers both "is this stock?"
 * and "what was it before I touched it?". */
function DefaultNote({ current, def }: { current: unknown; def: unknown }) {
  if (def === undefined) return null; // defaults not loaded (offline) — say nothing
  return isDefault(current, def) ? (
    <span className="font-mono text-[9px] normal-case tracking-[0.06em] text-faint">default</span>
  ) : (
    <span className="font-mono text-[9px] normal-case tracking-[0.06em] text-gold">
      default: {fmtDefault(def)}
    </span>
  );
}

/* ---------- account ---------- */
function CredForm({ label, onSubmit }: { label: string; onSubmit: (u: string, p: string) => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(u.trim(), p);
      }}
    >
      <Field label="Username">
        <TextInput value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" />
      </Field>
      <Field label="Password">
        <TextInput type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" />
      </Field>
      <Button variant="primary" type="submit">
        {label}
      </Button>
    </form>
  );
}

function Account({
  auth,
  onFlash,
  onChanged,
}: {
  auth: AuthStatus;
  onFlash: (m: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const login = async (u: string, p: string) => {
    try {
      const r = await api.login(u, p);
      token.set(r.token, r.username);
      onFlash(`Signed in as ${r.username}`);
      onChanged();
    } catch (e) {
      onFlash(String((e as Error).message), false);
    }
  };
  const createUser = async (u: string, p: string, thenLogin: boolean) => {
    try {
      await api.createUser(u, p);
      if (thenLogin) await login(u, p);
      else {
        onFlash(`User ${u} created`);
        onChanged();
      }
    } catch (e) {
      onFlash(String((e as Error).message), false);
    }
  };

  if (!auth.enabled) {
    return (
      <div>
        <p className="text-[12.5px] leading-relaxed text-muted">
          Open workspace — no sign-in, and every connector is shared. Create the first user to turn on
          sign-in and private connectors.
        </p>
        <details className="mt-2.5">
          <summary className="cursor-pointer text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
            Turn on sign-in
          </summary>
          <div className="mt-3">
            <CredForm label="Create user & sign in" onSubmit={(u, p) => createUser(u, p, true)} />
          </div>
        </details>
      </div>
    );
  }
  if (!auth.user) {
    return (
      <div className="rounded-lg bg-fill p-4">
        <CredForm label="Sign in" onSubmit={login} />
      </div>
    );
  }
  return (
    <div className="rounded-lg bg-fill p-4">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-accent-soft font-display text-[15px] font-bold uppercase text-accent">
          {auth.user[0]}
        </span>
        <div>
          <div className="text-[14px] font-semibold">{auth.user}</div>
          <div className="text-[12px] text-muted">Signed in</div>
        </div>
        <Button
          className="ml-auto"
          onClick={async () => {
            await api.logout();
            token.clear();
            onFlash("Signed out");
            onChanged();
          }}
        >
          Sign out
        </Button>
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
          Add a user
        </summary>
        <div className="mt-3">
          <CredForm label="Create user" onSubmit={(u, p) => createUser(u, p, false)} />
        </div>
      </details>
    </div>
  );
}

/* ---------- workspace settings ---------- */
function WorkspaceSettings({
  locked,
  onFlash,
  onChanged,
}: {
  locked: boolean;
  onFlash: (m: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [s, setS] = useState<Settings | null>(null);
  const [defs, setDefs] = useState<SettingDefaults | null>(null);
  const [msg, setMsg] = useState("");
  const [llmMsg, setLlmMsg] = useState("");
  const [llmOk, setLlmOk] = useState(true);
  useEffect(() => {
    api.settings().then(setS).catch(() => setS(null));
    // Best-effort: without defaults the form still works, it just can't mark them.
    api.settingDefaults().then(setDefs).catch(() => setDefs(null));
  }, []);
  if (!s) return <p className="text-[12px] text-muted">…</p>;

  const set = (path: string, value: unknown) => {
    setS((prev) => {
      if (!prev) return prev;
      const [group, key] = path.split(".");
      return { ...prev, [group]: { ...(prev as any)[group], [key]: value } } as Settings;
    });
  };
  const num = (v: string) => (v === "" ? null : Number(v));

  const testLLM = async () => {
    setLlmMsg("testing…");
    setLlmOk(true);
    try {
      const r = await api.testLLM({
        provider: s.llm.provider,
        model: s.llm.model || null,
        base_url: s.llm.base_url,
        max_tokens: s.llm.max_tokens,
        thinking: s.llm.thinking,
        thinking_budget: s.llm.thinking_budget,
        api_key_env: s.llm.api_key_env,
      });
      setLlmOk(r.ok);
      setLlmMsg(r.ok ? `✓ ${r.message}` : `✗ ${r.message}`);
    } catch (e) {
      setLlmOk(false);
      setLlmMsg(String((e as Error).message).slice(0, 200));
    }
  };

  const save = async () => {
    setMsg("saving…");
    try {
      await api.updateSettings({
        llm: {
          provider: s.llm.provider,
          model: s.llm.model || null,
          base_url: s.llm.base_url,
          max_tokens: s.llm.max_tokens,
          thinking: s.llm.thinking,
          thinking_budget: s.llm.thinking_budget,
          api_key_env: s.llm.api_key_env,
        },
        retrieval: {
          top_k: s.retrieval.top_k,
          min_score: s.retrieval.min_score,
          hybrid: s.retrieval.hybrid,
          contextual_chunks: s.retrieval.contextual_chunks,
          reranker: s.retrieval.reranker,
          graph_expansion: s.retrieval.graph_expansion,
        },
        chat: s.chat,
        embedding: { provider: s.embedding.provider, model: s.embedding.model || null, instruct: s.embedding.instruct },
        graph: { extract_triples: s.graph.extract_triples, entity_resolution: s.graph.entity_resolution },
        repos: { auto_agents_md: s.repos.auto_agents_md },
      });
      setMsg("Saved");
      onFlash("Workspace settings saved");
      onChanged();
    } catch (e) {
      setMsg(String((e as Error).message).slice(0, 80));
    }
  };

  // `def(path)` is the shipped default for a field; `note(path)` renders its marker and
  // `changed(group, keys)` counts a section's customized fields for the header badge.
  // Only the keys the drawer actually renders are counted — a badge pointing at a knob
  // with no control would be a dead end.
  const def = (path: string): unknown => {
    if (!defs) return undefined;
    const [group, key] = path.split(".");
    return (defs as any)[group]?.[key];
  };
  const cur = (path: string): unknown => {
    const [group, key] = path.split(".");
    return (s as any)[group]?.[key];
  };
  const note = (path: string) => <DefaultNote current={cur(path)} def={def(path)} />;
  const changed = (group: string, keys: string[]) =>
    defs ? keys.filter((k) => !isDefault(cur(`${group}.${k}`), def(`${group}.${k}`))).length : 0;

  return (
    <div>
      {locked && <p className="mb-2 text-[12px] text-muted">Sign in above to change workspace settings.</p>}
      <Group title="Language model" locked={locked} changed={changed("llm", LLM_KEYS)}>
        <Field label="Provider" note={note("llm.provider")}>
          <Select value={s.llm.provider} onChange={(e) => set("llm.provider", e.target.value)}>
            <option value="anthropic">anthropic</option>
            <option value="ollama">ollama</option>
            <option value="litellm">litellm</option>
          </Select>
        </Field>
        <Field
          label="Model"
          note={note("llm.model")}
          hint={s.llm.provider === "litellm" ? "a model name your proxy is configured to route" : undefined}
        >
          <TextInput value={s.llm.model ?? ""} placeholder="default for provider" onChange={(e) => set("llm.model", e.target.value)} />
        </Field>
        {s.llm.provider !== "anthropic" && (
          <Field
            label={s.llm.provider === "litellm" ? "LiteLLM proxy base URL" : "Ollama base URL"}
            note={note("llm.base_url")}
            hint={s.llm.provider === "litellm" ? "e.g. http://localhost:4000" : undefined}
          >
            <TextInput value={s.llm.base_url} onChange={(e) => set("llm.base_url", e.target.value)} />
          </Field>
        )}
        {s.llm.provider === "litellm" && (
          <Field label="API key env var" note={note("llm.api_key_env")} hint="env var holding the proxy bearer key; leave for keyless proxies">
            <TextInput value={s.llm.api_key_env} placeholder="LITELLM_API_KEY" onChange={(e) => set("llm.api_key_env", e.target.value)} />
          </Field>
        )}
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Max tokens" note={note("llm.max_tokens")}>
            <TextInput value={String(s.llm.max_tokens)} onChange={(e) => set("llm.max_tokens", num(e.target.value))} />
          </Field>
          <Field label="Thinking budget" note={note("llm.thinking_budget")}>
            <TextInput value={String(s.llm.thinking_budget)} onChange={(e) => set("llm.thinking_budget", num(e.target.value))} />
          </Field>
        </div>
        <Toggle
          checked={s.llm.thinking}
          onChange={(v) => set("llm.thinking", v)}
          label="Extended thinking (uses more tokens / quota)"
          note={note("llm.thinking")}
        />
        <div className="flex flex-wrap items-center gap-2.5">
          <Button onClick={testLLM}>Test connection</Button>
          <span className="text-[11px] text-faint">
            {s.llm.provider === "litellm"
              ? "Pings your LiteLLM proxy with the current (unsaved) values."
              : "One-token round-trip with the current (unsaved) provider."}
          </span>
        </div>
        {llmMsg && (
          <div className={cn("mt-2 font-mono text-[11px] leading-snug", llmOk ? "text-accent" : "text-danger")}>
            {llmMsg}
          </div>
        )}
      </Group>

      <Group title="Retrieval" locked={locked} changed={changed("retrieval", RETRIEVAL_KEYS)}>
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Top-K chunks" note={note("retrieval.top_k")}>
            <TextInput value={String(s.retrieval.top_k)} onChange={(e) => set("retrieval.top_k", num(e.target.value))} />
          </Field>
          <Field label="Grounding threshold" note={note("retrieval.min_score")} hint="0–1; higher = stricter">
            <TextInput value={String(s.retrieval.min_score)} onChange={(e) => set("retrieval.min_score", num(e.target.value))} />
          </Field>
        </div>
        <Toggle
          checked={s.retrieval.hybrid}
          onChange={(v) => set("retrieval.hybrid", v)}
          label="Hybrid dense + sparse search"
          note={note("retrieval.hybrid")}
          hint="Fuse vector similarity with BM25 full-text so exact tokens (error codes, ticket IDs, service names) still surface. Query-time."
        />
        <Toggle
          checked={s.retrieval.reranker !== "none"}
          onChange={(v) => set("retrieval.reranker", v ? "fastembed" : "none")}
          label="Cross-encoder reranker"
          note={note("retrieval.reranker")}
          hint="Re-scores the top candidates with a model that reads query + chunk together. Downloads ~80 MB on first use, then cached. Query-time."
        />
        <Toggle
          checked={s.retrieval.graph_expansion}
          onChange={(v) => set("retrieval.graph_expansion", v)}
          label="Graph-expansion retrieval"
          note={note("retrieval.graph_expansion")}
          hint="After grounding, surface documents one knowledge-graph hop away — the cross-source / multi-hop channel. Never turns a refusal into an answer. Query-time."
        />
        <Toggle
          checked={s.retrieval.contextual_chunks}
          onChange={(v) => set("retrieval.contextual_chunks", v)}
          label="Contextual chunking"
          note={note("retrieval.contextual_chunks")}
          hint="Prepend a source · title · path breadcrumb to each chunk before embedding, so its vector carries the context it was split from. Ingest-time — re-sync every source to take effect; may warrant a grounding-threshold retune."
        />
      </Group>

      <Group title="Knowledge graph" locked={locked} changed={changed("graph", GRAPH_KEYS)}>
        <Toggle
          checked={s.graph.extract_triples}
          onChange={(v) => set("graph.extract_triples", v)}
          label="Extract relationships from prose (LLM)"
          note={note("graph.extract_triples")}
          hint="Run an LLM relationship-extraction pass over ingested docs to enrich the graph. Costs one LLM call per qualifying document. Ingest-time — re-sync to apply. Deterministic extractors (deps, code structure, ticket refs) always run regardless."
        />
        <Toggle
          checked={s.graph.entity_resolution}
          onChange={(v) => set("graph.entity_resolution", v)}
          label="Entity resolution (merge duplicate names)"
          note={note("graph.entity_resolution")}
          hint="Before creating a new graph entity, check for a same-type near-duplicate (embedding candidates + LLM adjudication) and merge into it instead — e.g. a wiki's 'Webroot Connector' and a repo's 'AppRiver.Connector.Web' becoming one node. Ingest-time — re-sync to apply."
        />
      </Group>

      <Group title="Repositories" locked={locked} changed={changed("repos", REPOS_KEYS)}>
        <Toggle
          checked={s.repos.auto_agents_md}
          onChange={(v) => set("repos.auto_agents_md", v)}
          label="Auto-generate architecture brief on sync"
          note={note("repos.auto_agents_md")}
          hint="The first time a git/files repo is synced and has no brief yet, generate a principal-engineer AGENTS.md from its code structure + docs (one LLM pass, fired once — never on every sync). You can always generate/refresh one manually per connector, or with qj agents-md."
        />
      </Group>

      <Group title="Conversations" locked={locked} changed={changed("chat", CHAT_KEYS)}>
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Compress after (tokens)" note={note("chat.compress_after_est_tokens")}>
            <TextInput value={String(s.chat.compress_after_est_tokens)} onChange={(e) => set("chat.compress_after_est_tokens", num(e.target.value))} />
          </Field>
          <Field label="Keep recent msgs" note={note("chat.keep_recent_messages")}>
            <TextInput value={String(s.chat.keep_recent_messages)} onChange={(e) => set("chat.keep_recent_messages", num(e.target.value))} />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Tool-result cap (history)" note={note("chat.tool_result_max_chars")}>
            <TextInput value={String(s.chat.tool_result_max_chars)} onChange={(e) => set("chat.tool_result_max_chars", num(e.target.value))} />
          </Field>
          <Field label="Tool-result cap (live)" note={note("chat.live_tool_result_max_chars")}>
            <TextInput value={String(s.chat.live_tool_result_max_chars)} onChange={(e) => set("chat.live_tool_result_max_chars", num(e.target.value))} />
          </Field>
        </div>
        <Toggle
          checked={s.chat.learn_from_conversations}
          onChange={(v) => set("chat.learn_from_conversations", v)}
          label="Distill durable facts from chats into memory"
          note={note("chat.learn_from_conversations")}
        />
      </Group>

      <Group title="Embedding (advanced)" locked={locked} changed={changed("embedding", EMBEDDING_KEYS)}>
        <div className="my-2.5 rounded-sm bg-gold-soft p-3 text-[11.5px] leading-relaxed text-gold">
          <b className="text-gold-hi">Changing embeddings needs a re-index.</b> Existing vectors are in the
          current model's space — after changing this, restart the server and re-sync every source.
        </div>
        <Field label="Provider" note={note("embedding.provider")}>
          <Select value={s.embedding.provider} onChange={(e) => set("embedding.provider", e.target.value)}>
            <option value="fastembed">fastembed</option>
            <option value="ollama">ollama</option>
          </Select>
        </Field>
        <Field label="Model" note={note("embedding.model")}>
          <TextInput value={s.embedding.model ?? ""} placeholder="default for provider" onChange={(e) => set("embedding.model", e.target.value)} />
        </Field>
        <Toggle
          checked={s.embedding.instruct}
          onChange={(v) => set("embedding.instruct", v)}
          label="Asymmetric query instructions"
          note={note("embedding.instruct")}
          hint="Embed queries and passages with the model's task instruction (bge, nomic, e5) — how these models were trained, so it lifts recall. Re-check the grounding threshold after enabling. For bge it's query-time (no re-embed); for nomic/e5 passages change too — re-sync."
        />
      </Group>

      <div className="mt-4 flex items-center gap-2.5 pt-1">
        <Button variant="primary" onClick={save} disabled={locked}>
          Save settings
        </Button>
        <span className="font-mono text-[11px] text-muted">{msg}</span>
      </div>
    </div>
  );
}

/* ---------- connectors ---------- */
function Connectors({
  auth,
  onFlash,
  onChanged,
  onOpenArtifact,
  syncJobs,
  onOpenSync,
}: {
  auth: AuthStatus;
  onFlash: (m: string, ok?: boolean) => void;
  onChanged: () => void;
  onOpenArtifact?: (a: { title: string; markdown: string }) => void;
  syncJobs: SyncJob[];
  onOpenSync: (source: string, clean: boolean, autoStart?: boolean, kind?: "sync" | "cleanup") => void;
}) {
  const [rows, setRows] = useState<ConnectorRow[] | null>(null);
  const [types, setTypes] = useState<ConnectorType[]>([]);
  const [adding, setAdding] = useState<ConnectorType | "pick" | null>(null);

  const load = async () => {
    try {
      setRows(await api.connectors());
    } catch {
      setRows([]);
    }
  };
  useEffect(() => {
    load();
    api.connectorTypes().then(setTypes).catch(() => setTypes([]));
  }, []);

  if (adding === "pick") {
    return (
      <div>
        <Crumb onClick={() => setAdding(null)}>All connectors</Crumb>
        <div className="grid grid-cols-2 gap-2">
          {types.map((t) => (
            <button
              key={t.type}
              onClick={() => setAdding(t)}
              className="rounded-sm bg-fill p-3.5 text-left transition hover:-translate-y-px hover:bg-fill2"
            >
              <div className="text-[13.5px] font-semibold">{t.label}</div>
              <div className="mt-1 text-[11.5px] leading-snug text-muted">{t.blurb}</div>
            </button>
          ))}
        </div>
      </div>
    );
  }
  if (adding) {
    return (
      <ConnectorForm
        type={adding}
        canShare={auth.enabled && !!auth.user}
        onBack={() => setAdding("pick")}
        onCreated={(name) => {
          onFlash(`Connected ${name}. Sync it to start learning.`);
          setAdding(null);
          load();
          onChanged();
        }}
      />
    );
  }

  return (
    <div>
      {rows == null ? (
        <p className="text-[12px] text-muted">…</p>
      ) : rows.length === 0 ? (
        <p className="text-[12.5px] leading-relaxed text-muted">
          No connectors yet. Connect the systems this org lives in — code, tickets, wikis, deploys — and
          QuickJoiner learns from them.
        </p>
      ) : (
        rows.map((c) => (
          <ConnectorPlate key={c.name} c={c} types={types} auth={auth} reload={() => { load(); onChanged(); }}
                          onOpenArtifact={onOpenArtifact}
                          job={syncJobs.find((j) => j.source === c.name)} onOpenSync={onOpenSync} />
        ))
      )}
      {auth.enabled && !auth.user ? (
        <p className="mt-2 text-[12.5px] text-muted">Sign in above to add or manage connectors.</p>
      ) : (
        <Button variant="primary" className="mt-1 w-full" onClick={() => setAdding("pick")}>
          <Plug size={14} /> Connect a system
        </Button>
      )}
    </div>
  );
}

function Crumb({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick} className="mb-3.5 inline-flex items-center gap-1.5 rounded-full bg-fill px-3 py-1.5 text-[11.5px] font-medium text-muted transition hover:bg-fill2 hover:text-ink">
      <ChevronLeft size={12} /> {children}
    </button>
  );
}

function ConnectorPlate({
  c,
  types,
  auth,
  reload,
  onOpenArtifact,
  job,
  onOpenSync,
}: {
  c: ConnectorRow;
  types: ConnectorType[];
  auth: AuthStatus;
  reload: () => void;
  onOpenArtifact?: (a: { title: string; markdown: string }) => void;
  job?: SyncJob;
  onOpenSync: (source: string, clean: boolean, autoStart?: boolean, kind?: "sync" | "cleanup") => void;
}) {
  const [pmsg, setPmsg] = useState("");
  const [pok, setPok] = useState(true);
  const [armed, setArmed] = useState(false);
  const [briefing, setBriefing] = useState(false);
  const [armedWipe, setArmedWipe] = useState(false);
  const [editing, setEditing] = useState(false);
  const paused = job?.state === "paused";
  const syncing = job?.state === "running" || job?.state === "stopping" || paused;
  const ctype = types.find((t) => t.type === c.type);
  const label = ctype?.label ?? c.type;
  const isRepo = c.type === "git" || c.type === "files";
  const flash = (m: string, ok = true) => {
    setPmsg(m);
    setPok(ok);
  };

  return (
    <div className="mb-2.5 rounded-lg bg-fill p-4">
      <div className="flex items-center gap-3">
        <span className="flex h-[36px] w-[36px] flex-shrink-0 items-center justify-center rounded-full bg-fill2 text-muted">
          <Plug size={16} />
        </span>
        <div className="min-w-0">
          {c.can_manage ? (
            <button
              onClick={() => setEditing(true)}
              title="Edit this connector"
              className="truncate text-left text-[14.5px] font-semibold text-ink underline decoration-transparent underline-offset-2 transition hover:decoration-accent"
            >
              {c.name}
            </button>
          ) : (
            <div className="truncate text-[14.5px] font-semibold">{c.name}</div>
          )}
          <div className="mt-0.5 text-[11px] uppercase tracking-[0.06em] text-faint">{label}</div>
        </div>
        {syncing && (
          <button
            onClick={() => onOpenSync(c.name, Boolean(job?.clean))}
            title="Open the live sync log"
            className={cn(
              "flex flex-shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.12em] transition hover:brightness-110",
              paused ? "bg-gold-soft text-gold" : "bg-accent-soft text-accent",
            )}
          >
            <span className={cn("h-[6px] w-[6px] rounded-full", paused ? "bg-gold" : "animate-pulse bg-accent")} />
            {paused
              ? "paused"
              : job?.state === "stopping"
                ? "stopping"
                : job?.kind === "cleanup"
                  ? "cleaning"
                  : "syncing"}
          </button>
        )}
        <span
          className={cn(
            "ml-auto flex-shrink-0 rounded-full px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.12em]",
            c.shared || !c.owner ? "bg-accent-soft text-accent" : "bg-gold-soft text-gold",
          )}
        >
          {c.shared || !c.owner ? "shared" : "only you"}
        </span>
        {c.can_manage && (
          <IconButton
            className="flex-shrink-0 hover:bg-danger-soft hover:text-danger"
            title={armed ? "Click again to confirm removal" : "Remove connector and forget what it taught"}
            disabled={syncing}
            onClick={async () => {
              if (!armed) {
                setArmed(true);
                flash(
                  `Click the trash again to confirm — removes ${c.name} and cleans up its ${c.documents} learned document(s).`,
                );
                setTimeout(() => setArmed(false), 5000);
                return;
              }
              try {
                // Deleting purges by default: a removed connector's knowledge is
                // unreachable (source_id = type:name), so leaving it behind strands it.
                const r = await api.deleteConnector(c.name);
                reload();
                if (r.job) onOpenSync(c.name, false); // watch the cleanup job
              } catch (e) {
                flash(String((e as Error).message), false);
              }
            }}
          >
            <Trash2 size={15} className={armed ? "text-danger" : ""} />
          </IconButton>
        )}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-2.5 gap-y-1 font-mono text-[10.5px] tabular-nums text-faint">
        <span>{c.owner ? `by ${c.owner}` : "commons"}</span>
        <span>{c.documents} docs</span>
        <span>{c.last_sync ? `synced ${c.last_sync.slice(0, 16).replace("T", " ")}` : "never synced"}</span>
        <span>{schedLabel(c.sync_interval_minutes)}</span>
      </div>
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {ALL_MODES.map((mo) => (
          <span
            key={mo}
            className={cn(
              "rounded-full px-2.5 py-1 font-mono text-[8.5px] uppercase tracking-[0.1em]",
              c.modes.includes(mo) ? "bg-accent-soft text-accent" : "bg-fill text-faint",
            )}
          >
            {mo}
          </span>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1">
        <IconButton
          title="Test connection"
          onClick={async () => {
            flash("testing…");
            try {
              const r = await api.testConnector(c.name);
              flash(r.message || (r.ok ? "Reachable" : "Failed"), r.ok);
            } catch (e) {
              flash(String((e as Error).message), false);
            }
          }}
        >
          <Plug size={15} />
        </IconButton>
        <IconButton
          title={syncing ? "A sync is already running — open its log" : "Sync now"}
          onClick={() => onOpenSync(c.name, syncing ? Boolean(job?.clean) : false, !syncing)}
        >
          <RefreshCw size={15} className={syncing ? "animate-spin text-accent" : ""} />
        </IconButton>
        <IconButton
          className="hover:bg-danger-soft hover:text-danger disabled:opacity-35 disabled:hover:bg-transparent disabled:hover:text-muted"
          title={syncing ? "A sync is already running for this source" : "Clean re-sync — purge this source's documents, vectors and graph edges, then re-pull from scratch"}
          disabled={syncing}
          onClick={() => onOpenSync(c.name, true, true)}
        >
          <RotateCcw size={15} />
        </IconButton>
        <IconButton
          className="hover:bg-danger-soft hover:text-danger disabled:opacity-35 disabled:hover:bg-transparent disabled:hover:text-muted"
          disabled={syncing}
          title={
            armedWipe
              ? "Click again to confirm — this forgets everything this connector taught"
              : "Clean up — forget this connector's documents, vectors and graph edges, keeping it configured"
          }
          onClick={() => {
            if (!armedWipe) {
              setArmedWipe(true);
              flash(`Click again to confirm — forgets ${c.documents} document(s) but keeps ${c.name} configured.`);
              setTimeout(() => setArmedWipe(false), 5000);
              return;
            }
            setArmedWipe(false);
            onOpenSync(c.name, false, true, "cleanup");
          }}
        >
          <Eraser size={15} className={armedWipe ? "text-danger" : ""} />
        </IconButton>
        {isRepo && (
          <IconButton
            disabled={briefing}
            title="Generate a principal-engineer architecture brief from this repo's code structure + docs (no code is run). Refines an existing AGENTS.md if present."
            onClick={async () => {
              setBriefing(true);
              flash("generating architecture brief… (one LLM pass)");
              try {
                const r = await api.generateAgentsMd(c.name);
                flash("Architecture brief ready.", true);
                onOpenArtifact?.({ title: `${c.name} — Architecture Brief`, markdown: r.brief });
              } catch (e) {
                flash(String((e as Error).message), false);
              } finally {
                setBriefing(false);
              }
            }}
          >
            <FileText size={15} className={briefing ? "animate-pulse" : ""} />
          </IconButton>
        )}
        {c.can_manage && auth.enabled && c.owner && (
          <IconButton
            title={c.shared ? "Make private (only you)" : "Share with everyone"}
            onClick={async () => {
              try {
                await api.patchConnector(c.name, { shared: !c.shared });
                reload();
              } catch (e) {
                flash(String((e as Error).message), false);
              }
            }}
          >
            {c.shared ? <Lock size={15} /> : <Users size={15} />}
          </IconButton>
        )}
        {c.can_manage && (
          <Select
            title="Automatic sync schedule"
            className="ml-1 w-auto"
            value={c.sync_interval_minutes ? String(c.sync_interval_minutes) : ""}
            onChange={async (e) => {
              const v = e.target.value;
              try {
                await api.patchConnector(c.name, v === "" ? { clear_sync_interval: true } : { sync_interval_minutes: Number(v) });
                flash(v === "" ? "Set to manual" : `Scheduled: ${schedLabel(Number(v))} (effective on restart)`, true);
                reload();
              } catch (err) {
                flash(String((err as Error).message), false);
              }
            }}
          >
            {SYNC_OPTIONS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </Select>
        )}
      </div>
      {pmsg && <div className={cn("mt-2 font-mono text-[10.5px] leading-snug", pok ? "text-accent" : "text-danger")}>{pmsg}</div>}
      {editing && (
        <EditConnectorModal
          c={c}
          type={ctype}
          canShare={Boolean(auth.enabled && c.owner)}
          onSaved={() => {
            setEditing(false);
            reload();
            flash("Saved.", true);
          }}
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  );
}

function ConnectorForm({
  type,
  canShare,
  onBack,
  onCreated,
}: {
  type: ConnectorType;
  canShare: boolean;
  onBack: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [sync, setSync] = useState("");
  const [share, setShare] = useState(false);
  const [msg, setMsg] = useState("");

  const submit = async (skip: boolean) => {
    if (!name.trim()) {
      setMsg("Give this connector a name.");
      return;
    }
    const options: Record<string, unknown> = {};
    for (const f of type.fields) {
      const v = (vals[f.key] ?? "").trim();
      if (!v) {
        if (f.required) {
          setMsg(`${f.label} is required.`);
          return;
        }
        continue;
      }
      options[f.key] = f.list && v.includes(",") ? v.split(",").map((x) => x.trim()) : v;
    }
    setMsg(skip ? "saving…" : "testing connection…");
    try {
      await api.createConnector({
        name: name.trim(),
        type: type.type,
        options,
        skip_test: skip,
        sync_interval_minutes: sync ? Number(sync) : null,
        shared: canShare ? share : true,
      });
      onCreated(name.trim());
    } catch (e) {
      setMsg(String((e as Error).message));
    }
  };

  return (
    <div>
      <Crumb onClick={onBack}>Pick another system</Crumb>
      <Field label="Name *" hint="How this connector appears everywhere — pick something recognizable.">
        <TextInput value={name} placeholder={`e.g. ${type.type}-main`} onChange={(e) => setName(e.target.value)} />
      </Field>
      {type.fields.map((f) => {
        const hints = [
          f.help,
          f.secret && f.env ? `Safer: keep it out of config with env:${f.env}` : "",
          f.list ? "Several values? Separate with commas." : "",
        ].filter(Boolean);
        return (
          <Field key={f.key} label={`${f.label}${f.required ? " *" : ""}`} hint={hints.join(" — ")}>
            <TextInput
              type={f.secret ? "password" : "text"}
              placeholder={f.placeholder}
              value={vals[f.key] ?? ""}
              onChange={(e) => setVals((p) => ({ ...p, [f.key]: e.target.value }))}
            />
          </Field>
        );
      })}
      <Field label="Sync automatically" hint="Keep memory fresh on a schedule (needs the server running).">
        <Select value={sync} onChange={(e) => setSync(e.target.value)}>
          {SYNC_OPTIONS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </Select>
      </Field>
      {canShare && (
        <label className="my-3 flex items-start gap-2 text-[12.5px] text-muted">
          <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} className="mt-0.5 h-[15px] w-[15px] accent-[var(--accent)]" />
          Share with everyone — others can see, sync, and ask through this connector
        </label>
      )}
      <div className="mt-1 flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => submit(false)}>
          Test &amp; save
        </Button>
        <Button onClick={() => submit(true)}>Save without testing</Button>
      </div>
      {msg && <div className="mt-2 font-mono text-[10.5px] text-danger">{msg}</div>}
    </div>
  );
}
