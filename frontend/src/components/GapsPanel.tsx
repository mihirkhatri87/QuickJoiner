import { Lightbulb, Plug, Sparkles, X } from "lucide-react";
import type { GapCluster, GapsResponse } from "../types";
import { cn } from "./ui";

/* The knowledge-debt backlog: questions memory could not answer, clustered by topic.
 * Each cluster offers one-click remediation — connect a suggested source, teach the
 * answer, or dismiss. Violet is the "not-learned" token, matching the refusal color. */
export function GapsPanel({
  open,
  data,
  onClose,
  onConnect,
  onTeach,
  onDismiss,
}: {
  open: boolean;
  data: GapsResponse;
  onClose: () => void;
  onConnect: (type: string) => void;
  onTeach: (label: string) => void;
  onDismiss: (cluster: GapCluster) => void;
}) {
  if (!open) return null;

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
          <span className="h-[8px] w-[8px] flex-shrink-0 rounded-full bg-unknown shadow-[0_0_0_3px_var(--unknown-soft)]" />
          <div className="min-w-0 flex-1">
            <div className="text-[14px] font-semibold text-ink">Knowledge gaps</div>
            <div className="mt-0.5 text-[11.5px] text-muted">
              Questions memory couldn't answer yet — connect a source or teach the answer to close them.
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted transition hover:text-ink"
          >
            <X size={15} />
          </button>
        </div>

        <div className="scroll-thin flex flex-col gap-3 overflow-y-auto px-5 py-4">
          {data.clusters.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-12 text-center">
              <Sparkles size={22} className="text-gold" />
              <div className="text-[14px] font-medium text-ink">No open gaps</div>
              <div className="max-w-[360px] text-[12.5px] leading-relaxed text-muted">
                Every question asked so far was answered from learned memory. Refusals show up here as they happen.
              </div>
            </div>
          ) : (
            data.clusters.map((c) => (
              <div key={c.id} className="rounded-lg border border-fill2 bg-fill p-4">
                <div className="flex items-start gap-2.5">
                  <span className="mt-0.5 flex-shrink-0 rounded-full bg-unknown-soft px-2 py-0.5 font-mono text-[11px] font-semibold tabular-nums text-unknown">
                    {c.count}×
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="break-words text-[13.5px] font-medium text-ink">{c.label}</div>
                    {c.samples.length > 1 && (
                      <ul className="mt-1.5 flex flex-col gap-0.5">
                        {c.samples.slice(1).map((s, i) => (
                          <li key={i} className="truncate text-[11.5px] text-muted">
                            · {s}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                {c.entity_hints.length > 0 && (
                  <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                    <Lightbulb size={12} className="text-gold" />
                    {c.entity_hints.map((e) => (
                      <span
                        key={e.id}
                        title={e.type}
                        className="rounded-[5px] border border-gold-line bg-gold-soft px-2 py-0.5 font-mono text-[10px] text-gold"
                      >
                        {e.name}
                      </span>
                    ))}
                    <span className="text-[11px] text-muted">already known — may be related</span>
                  </div>
                )}

                <div className="mt-3 flex flex-wrap gap-2">
                  {c.suggested_connectors.map((type) => (
                    <button
                      key={type}
                      onClick={() => onConnect(type)}
                      className="inline-flex items-center gap-1.5 rounded-full bg-accent px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-accent-ink transition hover:bg-accent-hi"
                    >
                      <Plug size={12} /> Connect {type}
                    </button>
                  ))}
                  <button
                    onClick={() => onTeach(c.label)}
                    className="inline-flex items-center gap-1.5 rounded-full bg-fill2 px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-muted transition hover:text-ink"
                  >
                    <Sparkles size={12} /> Teach
                  </button>
                  <button
                    onClick={() => onDismiss(c)}
                    className={cn(
                      "ml-auto inline-flex items-center rounded-full px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em]",
                      "text-faint transition hover:text-muted",
                    )}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
