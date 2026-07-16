import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SyncJob } from "../types";
import { Button, cn } from "./ui";

/** Live view of a running sync: streams the job's log lines, and lets the user stop
 * it — the stop asks whether to clean up whatever was partially ingested, so an
 * interrupted sync never leaves the source (or the knowledge graph) half-populated. */
export function SyncLogModal({
  name,
  clean,
  onClose,
}: {
  name: string;
  clean: boolean;
  onClose: (finished: boolean) => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [job, setJob] = useState<SyncJob | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // start the job exactly once (StrictMode double-mount guard)
    started.current = true;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.startSync(name, clean);
        if (!cancelled) setJob(res.job);
        await api.streamSyncLogs(name, (e) => {
          if (cancelled) return;
          if (e.type === "log" && e.line) setLines((l) => [...l, e.line as string]);
          else if (e.type === "done" && e.job) setJob(e.job);
        });
      } catch (err) {
        if (!cancelled) setLines((l) => [...l, "✗ " + String((err as Error).message)]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [name, clean]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [lines]);

  const running = !job || job.state === "running" || job.state === "stopping";

  const doStop = async (cleanup: boolean) => {
    setStopping(true);
    setConfirmStop(false);
    try {
      await api.stopSync(name, cleanup);
    } catch {
      /* the stream will report the final state */
    }
  };

  const dot = running ? "bg-accent animate-pulse" : job?.state === "error" ? "bg-danger" : "bg-gold";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={() => !running && onClose(true)}
    >
      <div
        className="flex max-h-full w-full max-w-[720px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <span className={cn("h-[8px] w-[8px] flex-shrink-0 rounded-full", dot)} />
          <div className="text-[14px] font-semibold">
            {clean ? "Clean re-sync" : "Sync"} · {name}
          </div>
          <div className="ml-auto font-mono text-[10px] uppercase tracking-[0.12em] text-faint">
            {job?.state ?? "starting"}
          </div>
          {!running && (
            <button onClick={() => onClose(true)} className="text-faint transition hover:text-ink" aria-label="Close">
              <X size={16} />
            </button>
          )}
        </div>

        <div
          ref={boxRef}
          className="max-h-[52vh] min-h-[190px] overflow-y-auto bg-black/20 px-5 py-3 font-mono text-[12px] leading-relaxed text-muted"
        >
          {lines.length === 0 ? (
            <div className="text-faint">starting…</div>
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
            {running && !confirmStop && (
              <Button variant="danger" onClick={() => setConfirmStop(true)} disabled={stopping}>
                Stop
              </Button>
            )}
            {running && confirmStop && (
              <>
                <span className="self-center text-[12px] text-muted">Clean up partial data?</span>
                <Button variant="danger" onClick={() => doStop(true)}>
                  Stop &amp; clean up
                </Button>
                <Button onClick={() => doStop(false)}>Stop &amp; keep</Button>
                <Button onClick={() => setConfirmStop(false)}>Cancel</Button>
              </>
            )}
            {!running && (
              <Button variant="primary" onClick={() => onClose(true)}>
                Close
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
