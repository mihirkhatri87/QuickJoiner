import { ChevronRight, Plus, Radar } from "lucide-react";
import { useState } from "react";
import type { ProjectRow, SessionRow, SourceRow, Status } from "../types";
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
  onDistill,
  canDistill,
  sources,
  onManage,
  gapCount,
  onOpenGaps,
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
  onDistill: () => void;
  canDistill: boolean;
  sources: SourceRow[];
  onManage: () => void;
  gapCount: number;
  onOpenGaps: () => void;
}) {
  const [newProj, setNewProj] = useState("");
  const configured = sources.filter((s) => s.configured);
  const visible = sessions.filter((s) => s.title || s.est_tokens > 0).slice(0, 24);

  return (
    <>
      {open && <div className="fixed inset-0 z-[6] bg-black/50 md:hidden" onClick={onClose} />}
      <aside
        className={cn(
          "scroll-thin z-[7] flex w-[292px] flex-shrink-0 flex-col gap-6 overflow-y-auto rounded-lg bg-panel p-4 shadow-panel backdrop-blur-xl",
          "mb-3 ml-3 md:transition-[margin,opacity,visibility] md:duration-300",
          collapsed && "md:invisible md:pointer-events-none md:ml-[-304px] md:opacity-0",
          "max-md:fixed max-md:bottom-3 max-md:left-0 max-md:top-3 max-md:m-0 max-md:ml-3 max-md:transition-transform",
          open ? "max-md:translate-x-0" : "max-md:-translate-x-[110%]",
        )}
        aria-hidden={(collapsed && !open) || undefined}
      >
        {/* memory readout */}
        <div className="rounded-2xl bg-fill px-4 py-4">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.16em] text-faint">
            Learned memory
          </div>
          <div className="mt-2.5 flex flex-wrap items-baseline gap-1.5">
            <span className="font-mono text-[27px] font-semibold leading-none tabular-nums">
              {status?.stats.documents ?? 0}
            </span>
            <span className="text-[12.5px] text-muted">docs</span>
            <span className="ml-3 font-mono text-[27px] font-semibold leading-none tabular-nums">
              {status?.stats.chunks ?? 0}
            </span>
            <span className="text-[12.5px] text-muted">chunks</span>
          </div>
          <div className="mt-2 text-[12.5px] text-muted">
            from <span className="font-mono tabular-nums text-gold">{status?.stats.sources ?? 0}</span> connected
            system(s)
          </div>
        </div>

        {/* knowledge gaps */}
        <button
          onClick={onOpenGaps}
          className="flex items-center gap-2.5 rounded-2xl bg-fill px-4 py-3 text-left transition hover:bg-fill2"
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

        {/* project */}
        <div className="flex flex-col gap-2.5">
          <Eyebrow>Project</Eyebrow>
          <Select value={currentProject} onChange={(e) => onProject(e.target.value)} className="text-[13.5px]">
            <option value="">All memory · no project</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.session_count})
              </option>
            ))}
          </Select>
          <form
            className="flex gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (newProj.trim()) {
                onNewProject(newProj.trim());
                setNewProj("");
              }
            }}
          >
            <input
              value={newProj}
              onChange={(e) => setNewProj(e.target.value)}
              placeholder="New project"
              className="min-w-0 flex-1 rounded-full border border-transparent bg-fill px-3.5 py-2 text-[13px] outline-none transition placeholder:text-faint focus:border-[color-mix(in_srgb,var(--accent)_55%,transparent)] focus:bg-raised"
            />
            <Button type="submit">Add</Button>
          </form>
        </div>

        {/* conversations */}
        <div className="flex min-h-0 flex-col gap-2.5">
          <Eyebrow>Conversations</Eyebrow>
          {visible.length === 0 ? (
            <p className="px-1 text-[13px] leading-relaxed text-muted">
              No conversations yet — ask something below.
            </p>
          ) : (
            <div className="flex flex-col gap-0.5">
              {visible.map((s) => (
                <button
                  key={s.id}
                  onClick={() => onOpenSession(s.id)}
                  className={cn(
                    "rounded-xs px-3 py-2 text-left transition",
                    s.id === activeSession ? "bg-accent-soft" : "hover:bg-fill",
                  )}
                >
                  <div className={cn("truncate text-[13.5px]", s.id === activeSession && "text-accent")}>
                    {s.title || "(untitled)"}
                  </div>
                  <div className="mt-0.5 font-mono text-[10px] tabular-nums text-faint">
                    {s.project_id && !currentProject && <span className="text-gold">{s.project_id}</span>}
                    {s.project_id && !currentProject ? " · " : ""}
                    {s.updated_at.slice(0, 16).replace("T", " ")} · ~{s.est_tokens} tok
                  </div>
                </button>
              ))}
            </div>
          )}
          <div className="flex gap-1.5">
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

        {/* systems */}
        <div className="flex flex-col gap-2.5">
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
            <div className="flex flex-col">
              {configured.map((s) => (
                <div
                  key={s.id}
                  title={`${s.type} · ${s.documents} docs`}
                  className="flex items-center gap-2.5 rounded-xs px-3 py-2 transition hover:bg-fill"
                >
                  <span className="h-[7px] w-[7px] flex-shrink-0 rounded-full bg-gold shadow-[0_0_0_3px_var(--gold-soft)]" />
                  <span className="truncate text-[13.5px]">{s.name}</span>
                  <span className="ml-auto flex-shrink-0 font-mono text-[10.5px] tabular-nums text-faint">
                    {s.documents}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
