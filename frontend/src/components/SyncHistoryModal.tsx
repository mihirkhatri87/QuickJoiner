import { ChevronRight, RefreshCw, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { SyncJob } from "../types";
import { cn } from "./ui";

const TONE: Record<string, { dot: string; label: string }> = {
  running: { dot: "bg-accent animate-pulse", label: "syncing" },
  paused: { dot: "bg-gold", label: "paused" },
  stopping: { dot: "bg-accent animate-pulse", label: "stopping" },
  done: { dot: "bg-gold", label: "completed" },
  error: { dot: "bg-danger", label: "failed" },
  stopped: { dot: "bg-faint", label: "stopped" },
  interrupted: { dot: "bg-danger", label: "interrupted" },
};

function ago(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const isLive = (n: SyncJob) => n.state === "running" || n.state === "paused" || n.state === "stopping";

function runLine(n: SyncJob): string {
  if (isLive(n)) return n.phase ? `${n.phase}${n.percent != null ? ` · ${n.percent}%` : ""}` : "in progress";
  if (n.state === "error") return n.error || "failed";
  if (n.state === "interrupted") return "server restarted before it finished";
  if (n.kind === "reset" && n.state === "done") return "all memory wiped · connectors kept";
  if (n.kind === "cleanup" && n.state === "done") return "documents, vectors and graph removed";
  if (n.stats) {
    const s = n.stats;
    return `${s.added} added · ${s.updated} updated · ${s.skipped} unchanged` + (s.errors ? ` · ${s.errors} err` : "");
  }
  return n.state;
}

/** Full sync history (7-day window) grouped by connector, newest first within each group.
 * The ongoing/most-recent run for every connector is shown with its live stage + %, so this
 * is the one place to answer "what's the state of every connector's sync?". */
export function SyncHistoryModal({
  onClose,
  onOpenSync,
}: {
  onClose: () => void;
  onOpenSync: (source: string, clean: boolean) => void;
}) {
  const [items, setItems] = useState<SyncJob[] | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = () => api.notifications(168).then((r) => setItems(r.notifications)).catch(() => setItems([]));
  useEffect(() => {
    load();
    // Refresh while open so a running sync's % advances here too.
    const id = setInterval(load, 3000);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      clearInterval(id);
      window.removeEventListener("keydown", onKey);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Group by connector (source name), preserving the newest-first order the API returns.
  const groups: { source: string; runs: SyncJob[] }[] = [];
  const idx: Record<string, number> = {};
  for (const n of items ?? []) {
    if (idx[n.source] === undefined) {
      idx[n.source] = groups.length;
      groups.push({ source: n.source, runs: [] });
    }
    groups[idx[n.source]].runs.push(n);
  }

  const row = (n: SyncJob, primary: boolean) => {
    const tone = TONE[n.state] ?? TONE.stopped;
    const live = isLive(n);
    return (
      <button
        key={n.id}
        disabled={!n.live}
        onClick={() => n.live && onOpenSync(n.source, n.clean)}
        className={cn(
          "flex w-full items-start gap-2.5 rounded-sm px-3 py-2 text-left transition",
          n.live ? "hover:bg-fill" : "cursor-default",
          primary ? "" : "opacity-75",
        )}
      >
        <span className={cn("mt-[6px] h-[7px] w-[7px] flex-shrink-0 rounded-full", tone.dot)} />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-1.5">
            <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-faint">
              {n.clean ? "clean " : ""}
              {n.kind === "cleanup" ? "cleanup" : n.kind === "reset" ? "reset" : tone.label}
            </span>
            {live && n.percent != null && (
              <span className="font-mono text-[10px] tabular-nums text-accent">{n.percent}%</span>
            )}
            <span className="ml-auto font-mono text-[9.5px] tabular-nums text-faint">
              {ago(n.ended_at || n.started_at)}
            </span>
          </span>
          <span className="mt-0.5 block truncate font-mono text-[10.5px] tabular-nums text-muted">{runLine(n)}</span>
          {live && n.percent != null && (
            <span className="mt-1 block h-[3px] overflow-hidden rounded-full bg-fill2">
              <span
                className={cn("block h-full rounded-full", n.state === "paused" ? "bg-gold" : "bg-accent")}
                style={{ width: `${n.percent}%` }}
              />
            </span>
          )}
        </span>
      </button>
    );
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
          <div className="text-[14px] font-semibold">Sync history</div>
          <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-faint">last 7 days</div>
          <button onClick={load} title="Refresh" className="ml-auto text-faint transition hover:text-ink">
            <RefreshCw size={14} />
          </button>
          <button onClick={onClose} className="text-faint transition hover:text-ink" aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="scroll-thin overflow-y-auto p-3">
          {items === null ? (
            <p className="px-2 py-6 text-center text-[12.5px] text-muted">Loading…</p>
          ) : groups.length === 0 ? (
            <p className="px-2 py-6 text-center text-[12.5px] text-muted">No sync activity in the last 7 days.</p>
          ) : (
            groups.map((g) => {
              const [latest, ...rest] = g.runs;
              const open = expanded[g.source];
              return (
                <div key={g.source} className="mb-2 rounded-lg bg-fill p-1.5">
                  <div className="flex items-center gap-2 px-2 pt-1.5">
                    <span className="truncate text-[13px] font-semibold text-ink">{g.source}</span>
                    <span className="ml-auto font-mono text-[10px] text-faint">{g.runs.length} run{g.runs.length > 1 ? "s" : ""}</span>
                  </div>
                  {row(latest, true)}
                  {rest.length > 0 && (
                    <>
                      {open && rest.map((n) => row(n, false))}
                      <button
                        onClick={() => setExpanded((e) => ({ ...e, [g.source]: !e[g.source] }))}
                        className="flex items-center gap-1 px-3 py-1 text-[11px] text-faint transition hover:text-accent"
                      >
                        <ChevronRight size={12} className={cn("transition-transform", open && "rotate-90")} />
                        {open ? "Hide" : `${rest.length} earlier run${rest.length > 1 ? "s" : ""}`}
                      </button>
                    </>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
