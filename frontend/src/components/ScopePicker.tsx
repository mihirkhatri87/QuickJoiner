/* The scope chip beside the composer: "which slice of memory should this question use?"
 *
 * Deliberately a picker, not natural language. Resolving "only look at the Zix deck" with
 * the model would cost the very round-trip scoping exists to save — here the selection is
 * already concrete when the request leaves the browser, and the server turns it into a
 * filter before any search runs.
 *
 * Scope persists across questions in a conversation (you usually ask several things about
 * the same source) and is one click to clear. Empty = all of memory, which is the default
 * and the unchanged behaviour.
 */

import { Check, ChevronDown, Layers, Tag, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { AskScope, DocLabel, SourceRow } from "../types";
import { cn } from "./ui";

export const EMPTY_SCOPE: AskScope = { source_ids: [], doc_ids: [], tags: [] };

export function isScoped(scope: AskScope): boolean {
  return scope.source_ids.length > 0 || scope.doc_ids.length > 0 || scope.tags.length > 0;
}

/** Short human description of a scope — what the chip says, and what the sent message is
 * stamped with so the transcript still makes sense when read back later. */
export function describeScope(scope: AskScope, sources: SourceRow[]): string {
  const names = scope.source_ids.map(
    (id) => sources.find((s) => s.id === id)?.name ?? id.split(":").slice(1).join(":"),
  );
  const parts = [...names, ...scope.tags.map((t) => `#${t}`)];
  if (scope.doc_ids.length) parts.push(`${scope.doc_ids.length} document(s)`);
  return parts.join(" · ");
}

export function ScopePicker({
  scope,
  onChange,
  sources,
}: {
  scope: AskScope;
  onChange: (next: AskScope) => void;
  sources: SourceRow[];
}) {
  const [open, setOpen] = useState(false);
  const [labels, setLabels] = useState<DocLabel[]>([]);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    api.labels().then((r) => setLabels(r.labels)).catch(() => setLabels([]));
    const onDown = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  // Only tags are offered as scope entries: an `aka` names something, it doesn't group
  // things, so scoping "by aka" would be a confusing synonym for picking one document.
  const tags = useMemo(
    () => [...new Set(labels.filter((l) => l.kind === "tag").map((l) => l.value))].sort(),
    [labels],
  );
  // A source with nothing in it can't answer anything — offering it would be a trap.
  const withDocs = useMemo(() => sources.filter((s) => s.documents > 0), [sources]);

  const toggle = (key: "source_ids" | "tags", value: string) => {
    const list = scope[key];
    onChange({
      ...scope,
      [key]: list.includes(value) ? list.filter((v) => v !== value) : [...list, value],
    });
  };

  const scoped = isScoped(scope);

  return (
    <div className="relative" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={
          scoped
            ? "This question is limited to the selected sources — click to change"
            : "Ask across all learned memory. Narrow it to one connector, tag or document for a faster, less ambiguous answer."
        }
        className={cn(
          "inline-flex max-w-[280px] items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] transition",
          scoped ? "bg-accent-soft text-accent" : "bg-fill2 text-faint hover:text-muted",
        )}
      >
        <Layers size={12} className="flex-shrink-0" />
        <span className="truncate">{scoped ? describeScope(scope, sources) : "All memory"}</span>
        {scoped ? (
          <span
            role="button"
            aria-label="Clear scope"
            onClick={(e) => {
              e.stopPropagation();
              onChange(EMPTY_SCOPE);
            }}
            className="flex-shrink-0 hover:text-ink"
          >
            <X size={11} />
          </span>
        ) : (
          <ChevronDown size={11} className="flex-shrink-0" />
        )}
      </button>

      {open && (
        <div className="scroll-thin absolute bottom-[calc(100%+6px)] left-0 z-30 max-h-[320px] w-[300px] overflow-y-auto rounded-lg bg-raised2 py-1.5 shadow-lg">
          <div className="px-3 pb-1 pt-0.5 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
            Limit this question to
          </div>
          {withDocs.map((s) => (
            <button
              key={s.id}
              onClick={() => toggle("source_ids", s.id)}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-fill"
            >
              <span className="w-3 flex-shrink-0">
                {scope.source_ids.includes(s.id) && <Check size={12} className="text-accent" />}
              </span>
              <span className="truncate text-[12.5px] text-ink">{s.name}</span>
              <span className="ml-auto font-mono text-[9.5px] text-faint">{s.documents}</span>
            </button>
          ))}
          {tags.length > 0 && (
            <>
              <div className="mt-1 border-t border-fill2 px-3 pb-1 pt-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
                Tags
              </div>
              {tags.map((t) => (
                <button
                  key={t}
                  onClick={() => toggle("tags", t)}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-fill"
                >
                  <span className="w-3 flex-shrink-0">
                    {scope.tags.includes(t) && <Check size={12} className="text-accent" />}
                  </span>
                  <Tag size={10} className="flex-shrink-0 text-faint" />
                  <span className="truncate text-[12.5px] text-ink">{t}</span>
                </button>
              ))}
            </>
          )}
          {withDocs.length === 0 && tags.length === 0 && (
            <p className="px-3 py-2 text-[12px] text-muted">
              Nothing learned yet — sync a connector or ingest a document first.
            </p>
          )}
          {scoped && (
            <button
              onClick={() => onChange(EMPTY_SCOPE)}
              className="mt-1 w-full border-t border-fill2 px-3 py-2 text-left text-[12px] text-muted hover:text-ink"
            >
              Search all memory instead
            </button>
          )}
        </div>
      )}
    </div>
  );
}
