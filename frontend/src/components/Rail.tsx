import { ChevronRight, Plus, Radar, Trash2 } from "lucide-react";
import { useState } from "react";
import type { ProjectRow, SessionRow, SourceRow, Status, SyncJob } from "../types";
import { Button, cn, Eyebrow, Select } from "./ui";

export function Rail({
  open,
  collapsed,
  onClose,
  status,
  projects,
  currentProject,
  onProject,
  onNewProject,
  sessions,
  activeSession,
  onOpenSession,
  onNewConversation,
  onDeleteSession,
  onDeleteAll,
  onDistill,
  canDistill,
  sources,
  onManage,
  gapCount,
  onOpenGaps,
  syncJobs,
  onOpenSync,
}: {
  open: boolean;
  collapsed: boolean; // md+ only; mobile uses `open`
  onClose: () => void;
  status: Status | null;
  projects: ProjectRow[];
  currentProject: string;
  onProject: (id: string) => void;
  onNewProject: (name: string) => void;
  sessions: SessionRow[];
  activeSession: string | null;
  onOpenSession: (id: string) => void;
  onNewConversation: () => void;
  onDeleteSession: (id: string) => void;
  onDeleteAll: () => void;
  onDistill: () => void;
  canDistill: boolean;
  sources: SourceRow[];
  onManage: () => void;
  gapCount: number;
  onOpenGaps: () => void;
  syncJobs: SyncJob[];
  onOpenSync: (source: string, clean: boolean) => void;
}) {
  const [newProj, setNewProj] = useState("");
  const [addingProj, setAddingProj] = useState(false);
  const configured = sources.filter((s) => s.configured);
  // Newest job per source — a system that is syncing right now says so here, and clicking
  // it re-opens the live log (the same panel the Sync button opens).
  const jobBySource = new Map<string, SyncJob>();
  for (const j of syncJobs) if (!jobBySource.has(j.source)) jobBySource.set(j.source, j);
  const visible = sessions.filter((s) => s.title || s.est_tokens > 0).slice(0, 40);

  return (
    <>
      {open && <div className="fixed inset-0 z-[6] bg-black/50 md:hidden" onClick={onClose} />}
      <aside
        className={cn(
          // Fixed-height flex column: compact header, scrolling conversations, pinned systems.
          // min-h-0 lets the aside clamp to the row height so the inner list scrolls (not the page).
          "z-[7] flex min-h-0 w-[288px] flex-shrink-0 flex-col gap-3 overflow-hidden rounded-lg bg-panel p-3.5 shadow-panel backdrop-blur-xl",
          "mb-3 ml-3 md:transition-[margin,opacity,visibility] md:duration-300",
          collapsed && "md:invisible md:pointer-events-none md:ml-[-304px] md:opacity-0",
          "max-md:fixed max-md:bottom-3 max-md:left-0 max-md:top-3 max-md:m-0 max-md:ml-3 max-md:transition-transform",
          open ? "max-md:translate-x-0" : "max-md:-translate-x-[110%]",
        )}
        aria-hidden={(collapsed && !open) || undefined}
      >
        {/* memory readout — compact single card */}
        <div className="flex-shrink-0 rounded-xl bg-fill px-3.5 py-2.5">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-faint">Learned memory</div>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 font-mono text-[15px] tabular-nums">
            <span className="font-semibold">{status?.stats.documents ?? 0}</span>
            <span className="text-[11px] text-muted">docs</span>
            <span className="text-faint">·</span>
            <span className="font-semibold">{status?.stats.chunks ?? 0}</span>
            <span className="text-[11px] text-muted">chunks</span>
            <span className="text-faint">·</span>
            <span className="font-semibold text-gold">{status?.stats.sources ?? 0}</span>
            <span className="text-[11px] text-muted">systems</span>
          </div>
        </div>

        {/* knowledge gaps */}
        <button
          onClick={onOpenGaps}
          className="flex flex-shrink-0 items-center gap-2.5 rounded-xl bg-fill px-3.5 py-2.5 text-left transition hover:bg-fill2"
        >
          <Radar size={15} className="flex-shrink-0 text-unknown" />
          <span className="flex-1 text-[13px] font-medium text-ink">Knowledge gaps</span>
          {gapCount > 0 ? (
            <span className="flex-shrink-0 rounded-full bg-unknown-soft px-2 py-0.5 font-mono text-[11px] font-semibold tabular-nums text-unknown">
              {gapCount}
            </span>
          ) : (
            <span className="flex-shrink-0 font-mono text-[11px] text-faint">none</span>
          )}
        </button>

        {/* project — new-project form hides behind the + toggle so it's not permanent */}
        <div className="flex flex-shrink-0 flex-col gap-2">
          <Eyebrow
            action={
              <button
                onClick={() => setAddingProj((v) => !v)}
                title="New project"
                className="inline-flex items-center gap-1 text-[11.5px] font-medium text-accent hover:text-accent-hi"
              >
                <Plus size={13} /> New
              </button>
            }
          >
            Project
          </Eyebrow>
          <Select value={currentProject} onChange={(e) => onProject(e.target.value)} className="text-[13.5px]">
            <option value="">All memory · no project</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.session_count})
              </option>
            ))}
          </Select>
          {addingProj && (
            <form
              className="flex gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                if (newProj.trim()) {
                  onNewProject(newProj.trim());
                  setNewProj("");
                  setAddingProj(false);
                }
              }}
            >
              <input
                autoFocus
                value={newProj}
                onChange={(e) => setNewProj(e.target.value)}
                placeholder="New project name"
                className="min-w-0 flex-1 rounded-full border border-transparent bg-fill px-3.5 py-2 text-[13px] outline-none transition placeholder:text-faint focus:border-[color-mix(in_srgb,var(--accent)_55%,transparent)] focus:bg-raised"
              />
              <Button type="submit">Add</Button>
            </form>
          )}
        </div>

        {/* conversations — the flexible, independently-scrolling region */}
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <Eyebrow
            action={
              visible.length > 0 ? (
                <button
                  onClick={onDeleteAll}
                  title="Delete all conversations"
                  className="inline-flex items-center gap-1 text-[11.5px] font-medium text-faint transition hover:text-danger"
                >
                  <Trash2 size={12} /> Clear all
                </button>
              ) : undefined
            }
          >
            Conversations
          </Eyebrow>
          {visible.length === 0 ? (
            <p className="px-1 text-[13px] leading-relaxed text-muted">No conversations yet — ask something below.</p>
          ) : (
            <div className="scroll-thin -mr-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto pr-1">
              {visible.map((s) => (
                <div
                  key={s.id}
                  className={cn(
                    "group/row relative flex flex-shrink-0 items-center rounded-xs transition",
                    s.id === activeSession ? "bg-accent-soft" : "hover:bg-fill",
                  )}
                >
                  <button onClick={() => onOpenSession(s.id)} className="min-w-0 flex-1 px-3 py-1.5 text-left">
                    <div className={cn("truncate pr-5 text-[13px]", s.id === activeSession && "text-accent")}>
                      {s.title || "(untitled)"}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[9.5px] tabular-nums text-faint">
                      {s.project_id && !currentProject && <span className="text-gold">{s.project_id}</span>}
                      {s.project_id && !currentProject ? " · " : ""}
                      {s.updated_at.slice(0, 16).replace("T", " ")} · ~{s.est_tokens} tok
                    </div>
                  </button>
                  <button
                    onClick={() => onDeleteSession(s.id)}
                    title="Delete conversation"
                    aria-label="Delete conversation"
                    className="absolute right-1.5 top-1.5 hidden h-6 w-6 items-center justify-center rounded-full text-faint transition hover:bg-fill2 hover:text-danger group-hover/row:flex"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex flex-shrink-0 gap-1.5">
            <Button variant="primary" className="flex-1" onClick={onNewConversation}>
              <Plus size={15} /> New
            </Button>
            <Button
              disabled={!canDistill}
              onClick={onDistill}
              title="Extract a summary and durable facts from this conversation into memory"
            >
              Save
            </Button>
          </div>
        </div>

        {/* systems — pinned at the bottom, its own scroll if many */}
        <div className="flex flex-shrink-0 flex-col gap-2">
          <Eyebrow
            action={
              <button
                onClick={onManage}
                className="inline-flex items-center gap-1 text-[11.5px] font-medium text-accent hover:text-accent-hi"
              >
                Manage <ChevronRight size={12} />
              </button>
            }
          >
            Connected systems
          </Eyebrow>
          {configured.length === 0 ? (
            <p className="px-1 text-[13px] leading-relaxed text-muted">
              Nothing connected yet. Open <b>Manage</b> to add a system.
            </p>
          ) : (
            <div className="scroll-thin -mr-1 flex max-h-[148px] flex-col overflow-y-auto pr-1">
              {configured.map((s) => {
                const job = jobBySource.get(s.name);
                const paused = job?.state === "paused";
                const retrying = job?.state === "retrying";
                const syncing = job?.state === "running" || job?.state === "stopping" || paused || retrying;
                const watchable = Boolean(job?.live);
                const Row = watchable ? "button" : "div";
                return (
                  <Row
                    key={s.id}
                    {...(watchable
                      ? { onClick: () => onOpenSync(s.name, job!.clean), title: "Open the live sync log" }
                      : { title: `${s.type} · ${s.documents} docs` })}
                    className={cn(
                      "flex w-full flex-shrink-0 items-center gap-2.5 rounded-xs px-3 py-1.5 text-left transition hover:bg-fill",
                    )}
                  >
                    <span
                      className={cn(
                        "h-[7px] w-[7px] flex-shrink-0 rounded-full",
                        paused
                          ? "bg-gold shadow-[0_0_0_3px_var(--gold-soft)]"
                          : syncing
                            ? "animate-pulse bg-accent shadow-[0_0_0_3px_var(--accent-soft)]"
                            : "bg-gold shadow-[0_0_0_3px_var(--gold-soft)]",
                      )}
                    />
                    <span className="truncate text-[13px]">
                      {s.type === "uploads"
                        ? "Uploaded documents"
                        : s.type === "quickjoiner"
                          ? "QuickJoiner control"
                          : s.name}
                    </span>
                    <span
                      className={cn(
                        "ml-auto flex-shrink-0 font-mono text-[10.5px] tabular-nums",
                        paused ? "text-gold" : syncing ? "text-accent" : "text-faint",
                      )}
                    >
                      {paused
                        ? "paused"
                        : retrying
                          ? "retrying…"
                          : syncing
                            ? job?.percent != null
                              ? `${job.percent}%`
                              : job?.kind === "cleanup"
                                ? "cleaning…"
                                : "syncing…"
                            : s.documents}
                    </span>
                  </Row>
                );
              })}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
