import { X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { ConnectorRow, ConnectorType } from "../types";
import { Button, Field, Select, SYNC_OPTIONS, TextInput } from "./ui";

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
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState(false);

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
          {/* Name and type are shown, disabled, rather than hidden: the name IS the
              connector's identity — source_id is `type:name`, and every document id,
              vector, graph node, watermark and webhook URL is derived from it — so
              editing it here would orphan everything this connector has learned. */}
          <Field
            label="Name"
            hint="Fixed — this connector's learned documents, vectors, graph nodes and webhook URL are all keyed to it. To use a different name, clean up this connector and add it again."
          >
            <TextInput value={c.name} disabled readOnly className="cursor-not-allowed opacity-60" />
          </Field>
          {fields.length === 0 && (
            <div className="text-[12.5px] text-faint">This connector type has no editable options.</div>
          )}
          {fields.map((f) => {
            const hints = [
              f.help,
              f.secret ? "Leave unchanged to keep the stored secret." : "",
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
