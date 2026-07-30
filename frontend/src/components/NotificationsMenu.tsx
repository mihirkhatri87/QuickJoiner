import { Bell, CheckCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { SyncJob } from "../types";
import { cn, IconButton } from "./ui";

const SEEN_KEY = "qj_seen_notifications";
const SEEN_CAP = 300;

/** Read-state key. It includes the state, not just the job id, so one run notifies twice
 * — once when it starts running and again when it finishes — instead of a completion
 * being silently swallowed because the user glanced at the menu mid-sync. */
export const notifKey = (n: SyncJob) => `${n.id}:${n.state}`;

function loadSeen(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY) || "[]");
    return new Set(Array.isArray(raw) ? (raw as string[]) : []);
  } catch {
    return new Set();
  }
}

function saveSeen(seen: Set<string>) {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-SEEN_CAP)));
  } catch {
    /* private mode / quota — read-state is a nicety, never worth throwing over */
  }
}

function ago(iso: string | null): string {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

const TONE: Record<string, { dot: string; label: string }> = {
  running: { dot: "bg-accent animate-pulse", label: "syncing" },
  paused: { dot: "bg-gold", label: "paused" },
  retrying: { dot: "bg-gold animate-pulse", label: "retrying" },
  stopping: { dot: "bg-accent animate-pulse", label: "stopping" },
  done: { dot: "bg-gold", label: "completed" },
  error: { dot: "bg-danger", label: "failed" },
  stopped: { dot: "bg-faint", label: "stopped" },
  interrupted: { dot: "bg-danger", label: "interrupted" },
};

/** A cleanup is a different verb from a sync — "syncing" on a purge would read as the
 * opposite of what happened. */
function toneFor(n: SyncJob) {
  const base = TONE[n.state] ?? TONE.stopped;
  const running = n.state === "running" || n.state === "stopping";
  if (n.kind === "reset") {
    if (running) return { ...base, label: "resetting memory" };
    if (n.state === "done") return { ...base, label: "memory reset" };
    return base;
  }
  if (n.kind === "drain") {
    if (running) return { ...base, label: "mining relationships" };
    if (n.state === "done") return { ...base, label: "relationships mined" };
    return base;
  }
  if (n.kind !== "cleanup") return base;
  if (running) return { ...base, label: "cleaning up" };
  if (n.state === "done") return { ...base, label: "cleaned up" };
  return base;
}

function detail(n: SyncJob): string {
  const cleanup = n.kind === "cleanup";
  const reset = n.kind === "reset";
  const drain = n.kind === "drain";
  // A live run leads with its stage + %, so the menu answers "how far along?" at a glance.
  const stage = n.phase ? `${n.phase}${n.percent != null ? ` · ${n.percent}%` : ""}` : "";
  if (n.state === "retrying") return stage ? `network error · ${stage}` : "network error · retrying";
  if (n.state === "paused") return stage ? `paused · ${stage}` : `paused · started ${ago(n.started_at)}`;
  if (n.state === "running" || n.state === "stopping") {
    if (stage) return stage;
    if (reset) return `wiping all memory · started ${ago(n.started_at)}`;
    if (drain) return `mining queued documents · started ${ago(n.started_at)}`;
    return `${cleanup ? "forgetting this source" : "started"} ${ago(n.started_at)}`;
  }
  if (n.state === "error") return n.error || "failed";
  if (n.state === "interrupted") return "server restarted before it finished";
  if (reset && n.state === "done") return "all documents, vectors and graph wiped · connectors kept";
  if (cleanup && n.state === "done") return "documents, vectors and graph edges removed";
  if (n.stats && "documents" in n.stats) {
    const s = n.stats;
    return `${s.documents} document${s.documents === 1 ? "" : "s"} mined` +
      (s.text_only ? ` · ${s.text_only} partial` : "") +
      (s.missing_text ? ` · ${s.missing_text} still queued` : "");
  }
  if (n.stats) {
    const s = n.stats;
    return `${s.added} added · ${s.updated} updated · ${s.skipped} unchanged` +
      (s.errors ? ` · ${s.errors} errors` : "");
  }
  return n.state;
}

/** Bell menu: what the workspace has been doing over the last 24 hours — every sync that
 * is running now plus every one that finished, newest first. Read-state lives in
 * localStorage (per browser): the badge counts what hasn't been looked at, unseen rows
 * stay visually distinct while the menu is open, and closing the menu marks them read.
 */
export function NotificationsMenu({
  items,
  onOpenSync,
  onOpenHistory,
}: {
  items: SyncJob[];
  onOpenSync: (source: string, clean: boolean) => void;
  onOpenHistory: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState<Set<string>>(loadSeen);
  const ref = useRef<HTMLDivElement>(null);

  const unread = useMemo(() => items.filter((n) => !seen.has(notifKey(n))).length, [items, seen]);
  const active = items.filter(
    (n) => n.state === "running" || n.state === "stopping" || n.state === "retrying",
  );
  const lastDone = items.find((n) => n.state === "done");

  // Closing commits the read-state — while the menu is open the new rows stay highlighted
  // so the user can actually see what is new before it turns into history.
  const close = () => {
    setOpen(false);
    const next = new Set(seen);
    for (const n of items) next.add(notifKey(n));
    setSeen(next);
    saveSeen(next);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
    // `close` commits the *current* items/seen, so the listeners must be re-bound
    // whenever those change — otherwise a dismissal would write a stale read-state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, items, seen]);

  const markAllRead = () => {
    const next = new Set(seen);
    for (const n of items) next.add(notifKey(n));
    setSeen(next);
    saveSeen(next);
  };

  return (
    <div ref={ref} className="relative">
      <IconButton
        aria-label={unread ? `Activity — ${unread} new` : "Activity"}
        title={
          active.length
            ? `${active.length} sync${active.length > 1 ? "s" : ""} running`
            : lastDone
              ? `Last sync: ${lastDone.source} ${ago(lastDone.ended_at)}`
              : "Activity — last 24 hours"
        }
        onClick={() => (open ? close() : setOpen(true))}
        className={cn(open && "bg-fill2 text-ink")}
      >
        <span className="relative">
          <Bell size={17} className={active.length ? "text-accent" : undefined} />
          {unread > 0 && (
            <span className="absolute -right-[7px] -top-[6px] flex h-[15px] min-w-[15px] items-center justify-center rounded-full bg-accent px-[3px] font-mono text-[9px] font-semibold tabular-nums text-accent-ink">
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </span>
      </IconButton>

      {open && (
        <div className="absolute right-0 top-[42px] z-50 w-[340px] overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl">
          <div className="flex items-center gap-2 border-b border-fill2 px-4 py-2.5">
            <div className="text-[13px] font-semibold">Activity</div>
            <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-faint">last 24h</div>
            {unread > 0 && (
              <button
                onClick={markAllRead}
                title="Mark all as read"
                className="ml-auto inline-flex items-center gap-1 text-[11.5px] text-faint transition hover:text-accent"
              >
                <CheckCheck size={13} /> Mark read
              </button>
            )}
          </div>

          {active.length > 0 && (
            <div className="border-b border-fill2 bg-accent-soft px-4 py-2 text-[12px] text-accent">
              {active.length} {active.every((n) => n.kind === "cleanup") ? "cleanup" : "job"}
              {active.length > 1 ? "s" : ""} running now
            </div>
          )}

          <div className="scroll-thin max-h-[340px] overflow-y-auto py-1">
            {items.length === 0 ? (
              <p className="px-4 py-6 text-center text-[12.5px] leading-relaxed text-muted">
                No sync activity in the last 24 hours.
              </p>
            ) : (
              items.map((n) => {
                const isNew = !seen.has(notifKey(n));
                const tone = toneFor(n);
                const attachable = Boolean(n.live);
                const Row = attachable ? "button" : "div";
                return (
                  <Row
                    key={notifKey(n)}
                    {...(attachable
                      ? {
                          onClick: () => {
                            close();
                            onOpenSync(n.source, n.clean);
                          },
                          title: "Open the live sync log",
                        }
                      : {})}
                    className={cn(
                      "flex w-full items-start gap-2.5 px-4 py-2.5 text-left transition",
                      attachable && "hover:bg-fill",
                      // The one visual axis that separates new from already-viewed: an
                      // accent rail + lit background, with viewed rows dimmed.
                      isNew ? "border-l-2 border-accent bg-fill/60" : "border-l-2 border-transparent opacity-70",
                    )}
                  >
                    <span className={cn("mt-[6px] h-[7px] w-[7px] flex-shrink-0 rounded-full", tone.dot)} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline gap-1.5">
                        <span className="truncate text-[13px] font-medium text-ink">{n.source}</span>
                        <span className="flex-shrink-0 font-mono text-[9.5px] uppercase tracking-[0.1em] text-faint">
                          {n.clean ? "clean " : ""}
                          {tone.label}
                        </span>
                        <span className="ml-auto flex-shrink-0 font-mono text-[9.5px] tabular-nums text-faint">
                          {ago(n.ended_at || n.started_at)}
                        </span>
                      </span>
                      <span className="mt-0.5 block truncate font-mono text-[10.5px] tabular-nums text-muted">
                        {detail(n)}
                      </span>
                    </span>
                  </Row>
                );
              })
            )}
          </div>

          <button
            onClick={() => {
              close();
              onOpenHistory();
            }}
            className="w-full border-t border-fill2 px-4 py-2.5 text-center text-[12px] text-muted transition hover:bg-fill hover:text-accent"
          >
            View full history by connector →
          </button>
        </div>
      )}
    </div>
  );
}
