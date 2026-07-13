import { ChevronLeft, Plug, Settings as Gear, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, token } from "../api";
import type { AuthStatus, ConnectorRow, ConnectorType, Settings } from "../types";
import { Button, cn, Field, schedLabel, Select, SYNC_OPTIONS, TextInput } from "./ui";

const ALL_MODES = ["pull", "hooks", "live", "browser", "scrape"];

export function SettingsDrawer({
  open,
  onClose,
  onChanged,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
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
            <Connectors auth={auth} onFlash={flash} onChanged={onChanged} />
          </Section>
        </div>
      </aside>
    </>
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
  const [msg, setMsg] = useState("");
  useEffect(() => {
    api.settings().then(setS).catch(() => setS(null));
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
        retrieval: { top_k: s.retrieval.top_k, min_score: s.retrieval.min_score },
        chat: s.chat,
        embedding: { provider: s.embedding.provider, model: s.embedding.model || null },
      });
      setMsg("Saved");
      onFlash("Workspace settings saved");
      onChanged();
    } catch (e) {
      setMsg(String((e as Error).message).slice(0, 80));
    }
  };

  const Sub = ({ children }: { children: React.ReactNode }) => (
    <div className="mb-2.5 mt-5 border-b border-[var(--border)] pb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-accent first:mt-0">
      {children}
    </div>
  );

  return (
    <fieldset disabled={locked} className={locked ? "opacity-60" : ""}>
      {locked && <p className="mb-2 text-[12px] text-muted">Sign in above to change workspace settings.</p>}
      <Sub>Language model</Sub>
      <Field label="Provider">
        <Select value={s.llm.provider} onChange={(e) => set("llm.provider", e.target.value)}>
          <option value="anthropic">anthropic</option>
          <option value="ollama">ollama</option>
          <option value="litellm">litellm</option>
        </Select>
      </Field>
      <Field label="Model" hint={s.llm.provider === "litellm" ? "a model name your proxy is configured to route" : undefined}>
        <TextInput value={s.llm.model ?? ""} placeholder="default for provider" onChange={(e) => set("llm.model", e.target.value)} />
      </Field>
      {s.llm.provider !== "anthropic" && (
        <Field
          label={s.llm.provider === "litellm" ? "LiteLLM proxy base URL" : "Ollama base URL"}
          hint={s.llm.provider === "litellm" ? "e.g. http://localhost:4000" : undefined}
        >
          <TextInput value={s.llm.base_url} onChange={(e) => set("llm.base_url", e.target.value)} />
        </Field>
      )}
      {s.llm.provider === "litellm" && (
        <Field label="API key env var" hint="env var holding the proxy bearer key; leave for keyless proxies">
          <TextInput value={s.llm.api_key_env} placeholder="LITELLM_API_KEY" onChange={(e) => set("llm.api_key_env", e.target.value)} />
        </Field>
      )}
      <div className="grid grid-cols-2 gap-2.5">
        <Field label="Max tokens">
          <TextInput value={String(s.llm.max_tokens)} onChange={(e) => set("llm.max_tokens", num(e.target.value))} />
        </Field>
        <Field label="Thinking budget">
          <TextInput value={String(s.llm.thinking_budget)} onChange={(e) => set("llm.thinking_budget", num(e.target.value))} />
        </Field>
      </div>
      <label className="my-3 flex items-start gap-2 text-[12.5px] text-muted">
        <input type="checkbox" checked={s.llm.thinking} onChange={(e) => set("llm.thinking", e.target.checked)} className="mt-0.5 h-[15px] w-[15px] accent-[var(--accent)]" />
        Extended thinking (uses more tokens / quota)
      </label>

      <Sub>Retrieval</Sub>
      <div className="grid grid-cols-2 gap-2.5">
        <Field label="Top-K chunks">
          <TextInput value={String(s.retrieval.top_k)} onChange={(e) => set("retrieval.top_k", num(e.target.value))} />
        </Field>
        <Field label="Grounding threshold" hint="0–1; higher = stricter">
          <TextInput value={String(s.retrieval.min_score)} onChange={(e) => set("retrieval.min_score", num(e.target.value))} />
        </Field>
      </div>

      <Sub>Conversations</Sub>
      <div className="grid grid-cols-2 gap-2.5">
        <Field label="Compress after (tokens)">
          <TextInput value={String(s.chat.compress_after_est_tokens)} onChange={(e) => set("chat.compress_after_est_tokens", num(e.target.value))} />
        </Field>
        <Field label="Keep recent msgs">
          <TextInput value={String(s.chat.keep_recent_messages)} onChange={(e) => set("chat.keep_recent_messages", num(e.target.value))} />
        </Field>
      </div>
      <Field label="Tool-result cap (chars)">
        <TextInput value={String(s.chat.tool_result_max_chars)} onChange={(e) => set("chat.tool_result_max_chars", num(e.target.value))} />
      </Field>
      <label className="my-3 flex items-start gap-2 text-[12.5px] text-muted">
        <input type="checkbox" checked={s.chat.learn_from_conversations} onChange={(e) => set("chat.learn_from_conversations", e.target.checked)} className="mt-0.5 h-[15px] w-[15px] accent-[var(--accent)]" />
        Distill durable facts from chats into memory
      </label>

      <details className="mt-2">
        <summary className="cursor-pointer text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
          Embedding (advanced)
        </summary>
        <div className="my-2.5 rounded-sm bg-gold-soft p-3 text-[11.5px] leading-relaxed text-gold">
          <b className="text-gold-hi">Changing embeddings needs a re-index.</b> Existing vectors are in the
          current model's space — after changing this, restart the server and re-sync every source.
        </div>
        <Field label="Provider">
          <Select value={s.embedding.provider} onChange={(e) => set("embedding.provider", e.target.value)}>
            <option value="fastembed">fastembed</option>
            <option value="ollama">ollama</option>
          </Select>
        </Field>
        <Field label="Model">
          <TextInput value={s.embedding.model ?? ""} placeholder="default for provider" onChange={(e) => set("embedding.model", e.target.value)} />
        </Field>
      </details>

      <div className="mt-4 flex items-center gap-2.5 border-t border-[var(--border)] pt-3.5">
        <Button variant="primary" onClick={save}>
          Save settings
        </Button>
        <span className="font-mono text-[11px] text-muted">{msg}</span>
      </div>
    </fieldset>
  );
}

/* ---------- connectors ---------- */
function Connectors({
  auth,
  onFlash,
  onChanged,
}: {
  auth: AuthStatus;
  onFlash: (m: string, ok?: boolean) => void;
  onChanged: () => void;
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
          <ConnectorPlate key={c.name} c={c} types={types} auth={auth} reload={() => { load(); onChanged(); }} />
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
}: {
  c: ConnectorRow;
  types: ConnectorType[];
  auth: AuthStatus;
  reload: () => void;
}) {
  const [pmsg, setPmsg] = useState("");
  const [pok, setPok] = useState(true);
  const [armed, setArmed] = useState(false);
  const label = types.find((t) => t.type === c.type)?.label ?? c.type;
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
        <div>
          <div className="text-[14.5px] font-semibold">{c.name}</div>
          <div className="mt-0.5 text-[11px] uppercase tracking-[0.06em] text-faint">{label}</div>
        </div>
        <span
          className={cn(
            "ml-auto flex-shrink-0 rounded-full px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.12em]",
            c.shared || !c.owner ? "bg-accent-soft text-accent" : "bg-gold-soft text-gold",
          )}
        >
          {c.shared || !c.owner ? "shared" : "only you"}
        </span>
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
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Button
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
          Test
        </Button>
        <Button
          onClick={async () => {
            flash("syncing…");
            try {
              const r = await api.syncSource(c.name);
              flash(r.result, true);
              reload();
            } catch (e) {
              flash(String((e as Error).message), false);
            }
          }}
        >
          Sync now
        </Button>
        {c.can_manage && auth.enabled && c.owner && (
          <Button
            onClick={async () => {
              try {
                await api.patchConnector(c.name, { shared: !c.shared });
                reload();
              } catch (e) {
                flash(String((e as Error).message), false);
              }
            }}
          >
            {c.shared ? "Make private" : "Share"}
          </Button>
        )}
        {c.can_manage && (
          <Select
            title="Automatic sync schedule"
            className="w-auto"
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
        {c.can_manage && (
          <Button
            variant={armed ? "danger" : "ghost"}
            onClick={async () => {
              if (!armed) {
                setArmed(true);
                flash("Removes the config; learned memory stays.");
                setTimeout(() => setArmed(false), 4000);
                return;
              }
              try {
                await api.deleteConnector(c.name);
                reload();
              } catch (e) {
                flash(String((e as Error).message), false);
              }
            }}
          >
            {armed ? "Confirm?" : "Remove"}
          </Button>
        )}
      </div>
      {pmsg && <div className={cn("mt-2 font-mono text-[10.5px] leading-snug", pok ? "text-accent" : "text-danger")}>{pmsg}</div>}
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
