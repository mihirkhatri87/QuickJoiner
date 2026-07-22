import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SyncJob } from "../types";
import { Button, cn } from "./ui";

/** Live view of a sync job: streams the job's log lines, and lets the user stop it —
 * the stop asks whether to clean up whatever was partially ingested, so an interrupted
 * sync never leaves the source (or the knowledge graph) half-populated.
 *
 * The panel is a *viewer*, never a leash: closing it while the job runs only detaches
 * the viewer — the job lives on the server, on its own thread. Re-opening re-attaches,
 * and since `SyncManager.subscribe` replays the backlog, a reattached viewer sees the
 * whole run, including one that started before a page reload.
 *
 * `autoStart` distinguishes the two ways in: true = the user pressed Sync (start the job,
 * then watch it), false = the user clicked an existing job to watch it.
 */
export function SyncLogModal({
  name,
  clean,
  autoStart,
  kind = "sync",
  onClose,
  onJobChange,
}: {
  name: string;
  clean: boolean;
  autoStart: boolean;
  /** Which job to start when autoStart is set; also picks the panel's title. Watching an
   * existing job streams the same per-source log endpoint either way. */
  kind?: "sync" | "cleanup";
  onClose: () => void;
  onJobChange?: () => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [job, setJob] = useState<SyncJob | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const attached = useRef(false);
  const changed = useRef(onJobChange);
  changed.current = onJobChange;

  useEffect(() => {
    if (attached.current) return; // attach exactly once (StrictMode double-mount guard)
    attached.current = true;
    let cancelled = false;
    (async () => {
      try {
        if (autoStart) {
          const res = kind === "cleanup" ? await api.cleanupConnector(name) : await api.startSync(name, clean);
          if (!cancelled) setJob(res.job);
          changed.current?.();
        } else {
          // Attach path: load the current job state up front, so opening the panel on an
          // already-running or -paused job shows the right controls (Pause/Resume) rather
          // than waiting for the SSE `done` event that only arrives when it ends.
          try {
            const { syncs } = await api.listSyncs();
            const current = syncs.find((s) => s.source === name);
            if (!cancelled && current) setJob(current);
          } catch {
            /* non-fatal — the log stream still attaches below */
          }
        }
        await api.streamSyncLogs(name, (e) => {
          if (cancelled) return;
          if (e.type === "log" && e.line) setLines((l) => [...l, e.line as string]);
          else if (e.type === "done" && e.job) {
            setJob(e.job);
            changed.current?.();
          }
        });
      } catch (err) {
        if (!cancelled) setLines((l) => [...l, "✗ " + String((err as Error).message)]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [name, clean, autoStart, kind]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [lines]);

  // The SSE stream only carries log lines + a terminal `done`, so poll the live job while
  // it's active to keep the phase/% fresh. Stops polling once the job leaves an active
  // state (the `done` event already set the final job).
  const activeState =
    job === null ||
    job.state === "running" ||
    job.state === "paused" ||
    job.state === "retrying" ||
    job.state === "stopping";
  useEffect(() => {
    if (!activeState) return;
    let stop = false;
    const tick = async () => {
      try {
        const { syncs } = await api.listSyncs();
        const current = syncs.find((s) => s.source === name);
        if (!stop && current) setJob(current);
      } catch {
        /* transient — keep the last known job */
      }
    };
    const id = setInterval(tick, 2000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [name, activeState]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const paused = job?.state === "paused";
  const retrying = job?.state === "retrying";
  // "active" = the job still owns the source (incl. paused/retrying); drives the footer controls.
  const active =
    !job ||
    job.state === "running" ||
    job.state === "paused" ||
    job.state === "retrying" ||
    job.state === "stopping";
  const jobKind = job?.kind ?? kind;
  // Cleanup and reset are short, all-or-nothing purges — neither is pausable or stoppable.
  const isPurge = jobKind === "cleanup" || jobKind === "reset";
  const canPause = !isPurge && job?.state === "running";

  const doStop = async (cleanup: boolean) => {
    setStopping(true);
    setConfirmStop(false);
    try {
      await api.stopSync(name, cleanup);
    } catch {
      /* the stream will report the final state */
    }
  };

  const doPauseResume = async () => {
    try {
      // Update the job optimistically from the response — pause/resume emit no SSE `done`
      // event, so the stream won't flip the button state on its own.
      const res = paused ? await api.resumeSync(name) : await api.pauseSync(name);
      setJob(res.job);
      changed.current?.();
    } catch {
      /* a race with completion — the log stream carries the truth */
    }
  };

  const dot = paused
    ? "bg-gold"
    : retrying
      ? "bg-gold animate-pulse"
      : active
        ? "bg-accent animate-pulse"
        : job?.state === "error"
          ? "bg-danger"
          : "bg-gold";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-[720px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <span className={cn("h-[8px] w-[8px] flex-shrink-0 rounded-full", dot)} />
          <div className="truncate text-[14px] font-semibold">
            {jobKind === "reset"
              ? "Memory reset"
              : jobKind === "cleanup"
                ? "Cleanup"
                : (job?.clean ?? clean)
                  ? "Clean re-sync"
                  : "Sync"}{" "}
            · {name}
          </div>
          <div className="ml-auto flex-shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-faint">
            {job?.state ?? "starting"}
          </div>
          <button
            onClick={onClose}
            className="flex-shrink-0 text-faint transition hover:text-ink"
            title={active ? "Close — the job keeps running in the background" : "Close"}
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {active && (job?.phase || job?.percent != null) && (
          <div className="border-b border-fill2 px-5 py-2.5">
            <div className="flex items-baseline justify-between gap-3">
              <span className="truncate text-[12px] text-muted">
                {paused ? "Paused" : retrying ? "Retrying" : "Stage"}
                {job?.phase ? <span className="text-ink"> · {job.phase}</span> : null}
                {job?.phase_total ? (
                  <span className="ml-1 font-mono text-[11px] text-faint">
                    {job.phase_done}/{job.phase_total}
                  </span>
                ) : null}
              </span>
              {job?.percent != null && (
                <span className="flex-shrink-0 font-mono text-[12px] tabular-nums text-accent">{job.percent}%</span>
              )}
            </div>
            {/* Determinate bar when a % is known; a subtle indeterminate shimmer otherwise
                (streaming pulls with no upfront total still show liveness). */}
            <div className="mt-1.5 h-[4px] overflow-hidden rounded-full bg-fill2">
              {job?.percent != null ? (
                <div
                  className={cn("h-full rounded-full transition-[width] duration-500", paused ? "bg-gold" : "bg-accent")}
                  style={{ width: `${job.percent}%` }}
                />
              ) : (
                !paused && <div className="h-full w-1/3 animate-[indeterminate_1.4s_ease-in-out_infinite] rounded-full bg-accent/70" />
              )}
            </div>
          </div>
        )}

        <div
          ref={boxRef}
          className="max-h-[52vh] min-h-[190px] overflow-y-auto bg-black/20 px-5 py-3 font-mono text-[12px] leading-relaxed text-muted"
        >
          {lines.length === 0 ? (
            <div className="text-faint">{autoStart ? "starting…" : "attaching…"}</div>
          ) : (
            lines.map((l, i) => (
              <div key={i} className="whitespace-pre-wrap">
                {l}
              </div>
            ))
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-fill2 px-5 py-3">
          {job?.stats && (
            <div className="font-mono text-[11px] tabular-nums text-faint">
              {job.stats.added} added · {job.stats.updated} updated · {job.stats.skipped} unchanged ·{" "}
              {job.stats.chunks} chunks{job.stats.errors ? ` · ${job.stats.errors} errors` : ""}
            </div>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {active && !confirmStop && (
              <>
                <Button onClick={onClose}>Run in background</Button>
                {/* Cleanup and reset are short, all-or-nothing purges, so they offer neither
                    pause nor stop — a half-purge is exactly the inconsistent state they prevent. */}
                {!isPurge && (canPause || paused) && (
                  <Button onClick={doPauseResume}>{paused ? "Resume" : "Pause"}</Button>
                )}
                {!isPurge && (
                  <Button variant="danger" onClick={() => setConfirmStop(true)} disabled={stopping}>
                    Stop
                  </Button>
                )}
              </>
            )}
            {active && confirmStop && (
              <>
                <span className="self-center text-[12px] text-muted">Clean up partial data?</span>
                <Button variant="danger" onClick={() => doStop(true)}>
                  Stop &amp; clean up
                </Button>
                <Button onClick={() => doStop(false)}>Stop &amp; keep</Button>
                <Button onClick={() => setConfirmStop(false)}>Cancel</Button>
              </>
            )}
            {!active && (
              <Button variant="primary" onClick={onClose}>
                Close
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
