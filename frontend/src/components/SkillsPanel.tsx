import { ChevronRight, KeyRound, Sparkles, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SkillRow, SkillSecrets } from "../types";
import { Button, cn, IconButton, Select, TextInput } from "./ui";

/** The skill library: what packaged expertise this workspace has, whether each one is ready
 * FOR YOU, and — for the scoped ones — where you supply your own credentials.
 *
 * Two audiences in one panel, deliberately not split into two:
 *
 *   * everyone sees the library and fills in their own values, because a skill that reaches
 *     a real system runs as the person asking, not as the server;
 *   * an admin additionally installs, removes and re-scopes, because a skill can carry
 *     scripts and a script runs with the server's privileges.
 *
 * A skill missing its credentials is SHOWN, not hidden — the whole point is that you can see
 * the capability and be told exactly what to set, rather than wondering why the agent won't
 * do something you know it has.
 */
export function SkillsPanel({
  isAdmin,
  signedOut,
  onFlash,
}: {
  isAdmin: boolean;
  /** Auth is on and nobody is signed in: reading is fine, writing anything is not. */
  signedOut: boolean;
  onFlash: (msg: string, ok?: boolean) => void;
}) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  const [secrets, setSecrets] = useState<SkillSecrets>({ mine: [], workspace: [] });
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      const [rows, sec] = await Promise.all([api.skills(), api.skillSecrets()]);
      setSkills(rows);
      setSecrets(sec);
    } catch (e) {
      setSkills([]);
      onFlash(`Could not load skills: ${(e as Error).message}`, false);
    }
  };
  useEffect(() => {
    refresh();
  }, []);

  const install = async (file: File) => {
    setBusy(true);
    try {
      const r = await api.installSkill(file);
      onFlash(
        `${r.replaced ? "Replaced" : "Installed"} “${r.installed}” (${r.files} files).` +
          (r.skill?.required_env.length
            ? ` It needs ${r.skill.required_env.join(", ")} — set yours below.`
            : ""),
      );
      await refresh();
    } catch (e) {
      onFlash((e as Error).message, false);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  if (skills === null) return <div className="px-1 text-[12px] text-faint">Loading skills…</div>;

  const ready = skills.filter((s) => s.ready && s.enabled).length;

  return (
    <div>
      <p className="mb-3 px-1 text-[11.5px] leading-relaxed text-muted">
        Packaged expertise the agent can open when a question calls for it — query syntax,
        team conventions, a runbook. Written in the open{" "}
        <span className="text-ink">Agent Skills</span> format, so a skill you already wrote for
        Claude Code or Copilot works here unchanged.
      </p>

      {skills.length === 0 ? (
        <div className="mb-3 rounded-xl border border-dashed border-line/70 p-4 text-center text-[12px] text-muted">
          No skills yet.
          {isAdmin
            ? " Upload a .zip containing SKILL.md to add one."
            : " An admin can add them."}
        </div>
      ) : (
        <>
          <div className="mb-2 px-1 font-mono text-[10.5px] text-faint">
            {ready} of {skills.length} ready for you
          </div>
          {skills.map((s) => (
            <SkillPlate
              key={s.name}
              skill={s}
              isAdmin={isAdmin}
              signedOut={signedOut}
              secrets={secrets}
              onFlash={onFlash}
              onChanged={refresh}
            />
          ))}
        </>
      )}

      {isAdmin && (
        <>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) install(f);
            }}
          />
          <Button
            className="mt-1 w-full"
            disabled={busy || signedOut}
            onClick={() => fileRef.current?.click()}
          >
            <Upload size={14} />
            {busy ? "Installing…" : "Install a skill (.zip)"}
          </Button>
          <p className="mt-2 px-1 text-[11px] leading-snug text-faint">
            A skill may bundle scripts, and a script runs with the server's own privileges —
            which is why installing is admin-only while using one is open to everybody. Install
            what you would run yourself.
          </p>
        </>
      )}
    </div>
  );
}

/** One skill. Collapsed it answers "can I use this?"; expanded it answers "what do I do
 * about it?" — which for a normal user is the credential form and for an admin is also the
 * scope. */
function SkillPlate({
  skill,
  isAdmin,
  signedOut,
  secrets,
  onFlash,
  onChanged,
}: {
  skill: SkillRow;
  isAdmin: boolean;
  signedOut: boolean;
  secrets: SkillSecrets;
  onFlash: (msg: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [armedRemove, setArmedRemove] = useState(false);
  const scoped = skill.scope === "user";

  const state = !skill.enabled
    ? { label: "disabled", cls: "bg-fill2 text-faint" }
    : skill.ready
      ? { label: "ready", cls: "bg-accent-soft text-accent" }
      : { label: `needs ${skill.missing.length}`, cls: "bg-gold-soft text-gold" };

  const remove = async () => {
    try {
      await api.deleteSkill(skill.name);
      onFlash(`Removed “${skill.name}”.`);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    }
  };

  return (
    <div className="mb-2.5 rounded-xl border border-line/60 bg-fill/60 p-3.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-2.5 text-left"
      >
        <ChevronRight
          size={13}
          className={cn("mt-1 flex-shrink-0 text-muted transition-transform", open && "rotate-90")}
        />
        <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent">
          <Sparkles size={15} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-[13.5px] font-semibold">{skill.name}</span>
            <span
              className={cn(
                "ml-auto flex-shrink-0 rounded-full px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em]",
                state.cls,
              )}
            >
              {state.label}
            </span>
          </span>
          <span className="mt-0.5 block line-clamp-2 text-[11.5px] leading-snug text-muted">
            {skill.description || "(no description — the agent cannot tell when to use it)"}
          </span>
        </span>
      </button>

      {open && (
        <div className="mt-3 border-t border-line/50 pt-3">
          <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-faint">
            <span>{skill.origin}</span>
            <span>·</span>
            <span>{scoped ? "per-user credentials" : "open to everyone"}</span>
            {skill.scripts.length > 0 && (
              <>
                <span>·</span>
                <span>
                  {skill.scripts.length} script{skill.scripts.length === 1 ? "" : "s"}
                </span>
              </>
            )}
            {skill.references.length > 0 && (
              <>
                <span>·</span>
                <span>
                  {skill.references.length} reference{skill.references.length === 1 ? "" : "s"}
                </span>
              </>
            )}
          </div>

          {skill.warnings.map((w) => (
            <div key={w} className="mb-2 text-[11px] leading-snug text-gold">
              {w}
            </div>
          ))}

          {scoped && (
            <SecretsForm
              skill={skill}
              secrets={secrets}
              isAdmin={isAdmin}
              signedOut={signedOut}
              onFlash={onFlash}
              onChanged={onChanged}
            />
          )}

          {isAdmin && (
            <AdminControls
              skill={skill}
              signedOut={signedOut}
              onFlash={onFlash}
              onChanged={onChanged}
            />
          )}

          {isAdmin && skill.removable && (
            <div className="mt-3 flex justify-end">
              {armedRemove ? (
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted">Delete its files?</span>
                  <Button variant="danger" className="px-3 py-1 text-[11.5px]" onClick={remove}>
                    Remove
                  </Button>
                  <Button
                    className="px-3 py-1 text-[11.5px]"
                    onClick={() => setArmedRemove(false)}
                  >
                    Cancel
                  </Button>
                </div>
              ) : (
                <IconButton
                  title={`Remove ${skill.name}`}
                  aria-label={`Remove ${skill.name}`}
                  disabled={signedOut}
                  onClick={() => setArmedRemove(true)}
                >
                  <Trash2 size={14} />
                </IconButton>
              )}
            </div>
          )}
          {isAdmin && !skill.removable && (
            <p className="mt-3 text-[11px] leading-snug text-faint">
              Found in {skill.origin} storage, not this workspace — it belongs to that tooling,
              so QuickJoiner will not delete it. Disable it above instead.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Where a person supplies their own credentials for a scoped skill.
 *
 * Values are write-only everywhere: the API returns names, never content, so a key already
 * set shows as "set" with a Replace affordance rather than a populated box that would imply
 * we could read it back. */
function SecretsForm({
  skill,
  secrets,
  isAdmin,
  signedOut,
  onFlash,
  onChanged,
}: {
  skill: SkillRow;
  secrets: SkillSecrets;
  isAdmin: boolean;
  signedOut: boolean;
  onFlash: (msg: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [shared, setShared] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState("");

  const save = async (key: string) => {
    const value = draft[key];
    if (!value) return;
    setSaving(key);
    try {
      await api.setSkillSecret(key, value, shared[key] ? "workspace" : "user");
      setDraft((d) => ({ ...d, [key]: "" }));
      onFlash(`Stored ${key}.`);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    } finally {
      setSaving("");
    }
  };

  const clear = async (key: string, scope: "user" | "workspace") => {
    try {
      await api.deleteSkillSecret(key, scope);
      onFlash(`Removed ${key}.`);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    }
  };

  return (
    <div className="rounded-lg bg-fill2/50 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
        <KeyRound size={11} /> Your credentials
      </div>
      <p className="mb-2.5 text-[11px] leading-snug text-muted">
        This skill runs as you, with your own values — never the server's. They are encrypted
        at rest and no screen or API ever reads one back out.
      </p>
      {skill.required_env.map((key) => {
        const mine = secrets.mine.includes(key);
        const ws = secrets.workspace.includes(key);
        return (
          <div key={key} className="mb-2.5">
            <div className="mb-1 flex items-center gap-2">
              <span className="font-mono text-[11px] text-ink">{key}</span>
              {mine && (
                <span className="rounded-full bg-accent-soft px-1.5 py-px font-mono text-[9px] uppercase text-accent">
                  yours
                </span>
              )}
              {ws && !mine && (
                <span className="rounded-full bg-fill2 px-1.5 py-px font-mono text-[9px] uppercase text-muted">
                  workspace
                </span>
              )}
              {(mine || (ws && isAdmin)) && (
                <button
                  type="button"
                  disabled={signedOut}
                  onClick={() => clear(key, mine ? "user" : "workspace")}
                  className="ml-auto text-[10.5px] text-muted underline-offset-2 transition hover:text-danger hover:underline disabled:opacity-40"
                >
                  clear {mine ? "mine" : "workspace"}
                </button>
              )}
            </div>
            <div className="flex gap-1.5">
              <TextInput
                type="password"
                autoComplete="off"
                placeholder={mine || ws ? "Replace…" : "Paste the value"}
                value={draft[key] ?? ""}
                disabled={signedOut}
                onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
                onKeyDown={(e) => {
                  if (e.key === "Enter") save(key);
                }}
              />
              <Button
                variant="primary"
                className="flex-shrink-0 px-3 py-1.5 text-[12px]"
                disabled={!draft[key] || saving === key || signedOut}
                onClick={() => save(key)}
              >
                {saving === key ? "…" : "Save"}
              </Button>
            </div>
            {isAdmin && (
              <label className="mt-1 flex items-center gap-1.5 text-[10.5px] text-faint">
                <input
                  type="checkbox"
                  checked={Boolean(shared[key])}
                  disabled={signedOut}
                  onChange={(e) => setShared((s) => ({ ...s, [key]: e.target.checked }))}
                  className="h-3 w-3 accent-[var(--accent)]"
                />
                Set workspace-wide — a shared endpoint or tenant id everyone inherits. Anyone's
                own value still wins.
              </label>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The admin half: what this skill requires, and whether it is on at all.
 *
 * `detected_env` is offered as a one-click suggestion because detection reads a skill's
 * scripts and cannot tell a credential from a tuning flag — a real skill turned up nine
 * "requirements", four of which had defaults. Trimming the list is expected, so it is a
 * plain editable field rather than something you have to fight. */
function AdminControls({
  skill,
  signedOut,
  onFlash,
  onChanged,
}: {
  skill: SkillRow;
  signedOut: boolean;
  onFlash: (msg: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [required, setRequired] = useState(skill.required_env.join(", "));
  const [scope, setScope] = useState(skill.scope);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setRequired(skill.required_env.join(", "));
    setScope(skill.scope);
  }, [skill.required_env, skill.scope]);

  const dirty =
    scope !== skill.scope || required !== skill.required_env.join(", ");

  const save = async () => {
    setSaving(true);
    try {
      await api.updateSkill(skill.name, {
        scope,
        required_env: required.split(",").map((s) => s.trim()).filter(Boolean),
      });
      onFlash(`Updated “${skill.name}”.`);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (enabled: boolean) => {
    try {
      await api.updateSkill(skill.name, { enabled });
      onFlash(enabled ? `Enabled “${skill.name}”.` : `Disabled “${skill.name}”.`);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    }
  };

  const unlisted = skill.detected_env.filter((n) => !skill.required_env.includes(n));

  return (
    <div className="mt-3 rounded-lg bg-fill2/50 p-3">
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
        Configuration
      </div>
      <label className="mb-2.5 flex items-center gap-2 text-[12px]">
        <input
          type="checkbox"
          checked={skill.enabled}
          disabled={signedOut}
          onChange={(e) => toggle(e.target.checked)}
          className="h-[15px] w-[15px] accent-[var(--accent)]"
        />
        <span className="text-ink">Available to the agent</span>
      </label>

      <div className="mb-2">
        <div className="mb-1 text-[11px] text-muted">Who supplies its values</div>
        <Select value={scope} disabled={signedOut} onChange={(e) => setScope(e.target.value)}>
          <option value="open">Open — anyone can run it, no credentials</option>
          <option value="user">Per user — each person supplies their own</option>
        </Select>
      </div>

      <div className="mb-2">
        <div className="mb-1 text-[11px] text-muted">
          Required values <span className="text-faint">(comma-separated; ignored when open)</span>
        </div>
        <TextInput
          value={required}
          disabled={signedOut}
          placeholder="DEPLOY_TOKEN, DEPLOY_URL"
          onChange={(e) => setRequired(e.target.value)}
        />
        {unlisted.length > 0 && (
          <div className="mt-1.5 text-[10.5px] leading-snug text-faint">
            Its scripts also read{" "}
            <span className="font-mono text-muted">{unlisted.join(", ")}</span> —{" "}
            <button
              type="button"
              disabled={signedOut}
              onClick={() =>
                setRequired([...skill.required_env, ...unlisted].filter(Boolean).join(", "))
              }
              className="underline underline-offset-2 transition hover:text-ink disabled:opacity-40"
            >
              add them
            </button>{" "}
            if they are credentials rather than settings with defaults.
          </div>
        )}
      </div>

      <Button
        variant="primary"
        className="px-3 py-1.5 text-[12px]"
        disabled={!dirty || saving || signedOut}
        onClick={save}
      >
        {saving ? "Saving…" : "Save configuration"}
      </Button>
    </div>
  );
}
