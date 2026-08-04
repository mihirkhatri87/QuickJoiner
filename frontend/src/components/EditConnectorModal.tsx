import { X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { ConnectorRow, ConnectorType } from "../types";
import { Button, Field, Select, SYNC_OPTIONS, TextArea, TextInput } from "./ui";

/** Full edit of a configured connector: every option field (secrets kept unless
 * retyped), sharing, and the sync schedule. Name and type are the connector's
 * identity and stay read-only. Prefilled from the (secret-masked) stored config. */
export function EditConnectorModal({
  c,
  type,
  canShare,
  onSaved,
  onClose,
}: {
  c: ConnectorRow;
  type: ConnectorType | undefined;
  canShare: boolean;
  onSaved: () => void;
  onClose: () => void;
}) {
  const fields = type?.fields ?? [];
  const [vals, setVals] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of fields) {
      const v = c.options[f.key];
      init[f.key] = Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
    }
    return init;
  });
  const [share, setShare] = useState(c.shared);
  const [sync, setSync] = useState(c.sync_interval_minutes ? String(c.sync_interval_minutes) : "");
  const [name, setName] = useState(c.name);
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState(false);

  // The name keys everything a connector ingests, so it's only editable before the first
  // sync — i.e. while it has 0 learned documents. After that, clean up to rename.
  const canRename = c.documents === 0;
  const rename = canRename && name.trim() && name.trim() !== c.name;

  const save = async () => {
    // Send every field: the API keeps a secret sent back as its mask (•••), deletes a
    // field cleared to empty, and sets anything else — so this round-trips faithfully.
    const options: Record<string, unknown> = {};
    for (const f of fields) {
      const raw = (vals[f.key] ?? "").trim();
      options[f.key] = f.list && raw.includes(",") ? raw.split(",").map((x) => x.trim()) : raw;
    }
    setSaving(true);
    setMsg("saving…");
    try {
      await api.patchConnector(c.name, {
        options,
        ...(rename ? { name: name.trim() } : {}),
        ...(canShare ? { shared: share } : {}),
        ...(sync ? { sync_interval_minutes: Number(sync) } : { clear_sync_interval: true }),
      });
      onSaved();
    } catch (e) {
      setMsg(String((e as Error).message));
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-[560px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <div className="text-[14px] font-semibold">Edit {c.name}</div>
          <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-faint">{type?.label ?? c.type}</div>
          <button onClick={onClose} className="ml-auto text-faint transition hover:text-ink" aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4">
          {/* The name IS the connector's identity — source_id is `type:name`, and every
              document id, vector, graph node, watermark and webhook URL is derived from it.
              So it's only editable before the first sync (0 documents), when nothing is
              keyed to it yet; after that, renaming would orphan everything it learned. */}
          <Field
            label="Name"
            hint={
              canRename
                ? "Editable only until the first sync — the name keys everything this connector ingests."
                : "Fixed — this connector's learned documents, vectors, graph nodes and webhook URL are all keyed to it. Clean it up first to free the name for a rename."
            }
          >
            {canRename ? (
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder={c.name} />
            ) : (
              <TextInput value={c.name} disabled readOnly className="cursor-not-allowed opacity-60" />
            )}
          </Field>
          {fields.length === 0 && (
            <div className="text-[12.5px] text-faint">This connector type has no editable options.</div>
          )}
          {fields.map((f) => {
            // Identity-shaping fields (lock_after_sync, e.g. "also known as") follow the
            // same rule as the name: editable only before the first sync.
            const locked = Boolean(f.lock_after_sync) && !canRename;
            const hints = [
              locked ? "Fixed after the first sync — clean up this connector to change it." : f.help,
              f.secret ? "Leave unchanged to keep the stored secret." : "",
              !locked && f.list ? "Several values? Separate with commas." : "",
            ].filter(Boolean);
            return (
              <Field key={f.key} label={`${f.label}${f.required ? " *" : ""}`} hint={hints.join(" — ")}>
                {f.multiline ? (
                  <TextArea
                    placeholder={f.placeholder}
                    value={vals[f.key] ?? ""}
                    disabled={locked}
                    readOnly={locked}
                    className={locked ? "cursor-not-allowed opacity-60" : undefined}
                    onChange={(e) => setVals((p) => ({ ...p, [f.key]: e.target.value }))}
                  />
                ) : (
                  <TextInput
                    type={f.secret ? "password" : "text"}
                    placeholder={f.placeholder}
                    value={vals[f.key] ?? ""}
                    disabled={locked}
                    readOnly={locked}
                    className={locked ? "cursor-not-allowed opacity-60" : undefined}
                    onChange={(e) => setVals((p) => ({ ...p, [f.key]: e.target.value }))}
                  />
                )}
              </Field>
            );
          })}
          <Field label="Sync automatically" hint="Keep memory fresh on a schedule (effective on restart).">
            <Select value={sync} onChange={(e) => setSync(e.target.value)}>
              {SYNC_OPTIONS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </Select>
          </Field>
          {canShare && (
            <label className="my-2 flex items-start gap-2 text-[12.5px] text-muted">
              <input
                type="checkbox"
                checked={share}
                onChange={(e) => setShare(e.target.checked)}
                className="mt-0.5 h-[15px] w-[15px] accent-[var(--accent)]"
              />
              Share with everyone — others can see, sync, and ask through this connector
            </label>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-fill2 px-5 py-3">
          {msg && <div className="font-mono text-[10.5px] text-danger">{msg}</div>}
          <div className="ml-auto flex gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" onClick={save} disabled={saving}>
              Save changes
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
