/* The reasoning trace as a step-by-step timeline rather than a wall of text.
 *
 * The agent's run is genuinely a sequence — it thinks, calls a tool, reads what came
 * back, thinks again — and the SSE stream delivers those events in that real order.
 * The old view threw the ordering away: reasoning went into one <pre> blob and tool
 * calls into a separate row of pills, so you could see THAT `search_memory` ran but
 * never what it searched for, what it found, or which thought led to it.
 *
 * Each step here is one row on a vertical rail: an icon, a bold title, and a
 * one-line detail, expandable for the full text (a thought's whole paragraph, an
 * action's arguments and a preview of its result).
 *
 * Two honesty rules govern this file, since a trace that embellishes is worse than
 * no trace at all:
 *   - Thought titles are the model's OWN segmentation (its `**bold**`/`##` markers),
 *     never invented. A blob with no markers stays one step titled "Reasoning".
 *   - A result preview is bounded server-side and says so; `chars` carries the true
 *     size, so a clipped preview is never presented as the whole output.
 */
import {
  Boxes, ChevronDown, Database, FileText, GitBranch, Globe, Layers, Loader2, Network,
  Plug, Route, Search, Sparkles, Terminal, TriangleAlert, Waypoints,
} from "lucide-react";
import { useState } from "react";
import type { ToolCallEvent, ToolResultEvent } from "../types";
import { cn } from "./ui";

/** One entry in the run, in the order it happened. A `thought` accumulates streamed
 * reasoning deltas; an `action` is a tool call, awaiting its paired result. */
export type TraceStep =
  | { kind: "thought"; text: string }
  /** A progress line from a long-running operation that isn't a tool call — the
   * `/scrape` crawl log. One row per line, since each is a discrete event. */
  | { kind: "note"; text: string }
  | { kind: "action"; id: string; tool: string; args: Record<string, string>; result?: ToolResultEvent };

/* -- reducers (pure; the SSE handler in App.tsx is the only caller) ------------- */

/** Append a streamed reasoning delta, extending the trailing thought so consecutive
 * deltas stay one step, and starting a new one after an intervening action. */
export function appendThought(trace: TraceStep[], delta: string): TraceStep[] {
  const last = trace[trace.length - 1];
  if (last?.kind === "thought") {
    return [...trace.slice(0, -1), { kind: "thought", text: last.text + delta }];
  }
  return [...trace, { kind: "thought", text: delta }];
}

/** Append a standalone progress line as its own step (never merged into a thought). */
export function pushNote(trace: TraceStep[], text: string): TraceStep[] {
  return [...trace, { kind: "note", text }];
}

export function startAction(trace: TraceStep[], call: ToolCallEvent): TraceStep[] {
  return [...trace, { kind: "action", id: call.id, tool: call.name, args: call.args || {} }];
}

/** Attach a result to its call. Paired by id; matching on the trailing unfinished
 * call of the same name is the fallback, since a provider that omits or rewrites
 * ids must not leave a step spinning forever. */
export function finishAction(trace: TraceStep[], res: ToolResultEvent): TraceStep[] {
  let idx = trace.findIndex((s) => s.kind === "action" && s.id === res.id && !s.result);
  if (idx < 0) {
    for (let i = trace.length - 1; i >= 0; i--) {
      const s = trace[i];
      if (s.kind === "action" && s.tool === res.name && !s.result) { idx = i; break; }
    }
  }
  if (idx < 0) return trace;
  const step = trace[idx] as Extract<TraceStep, { kind: "action" }>;
  return [...trace.slice(0, idx), { ...step, result: res }, ...trace.slice(idx + 1)];
}

/* -- presentation -------------------------------------------------------------- */

/** Split a thought blob on the model's own section markers (`**Title**` or `## Title`
 * on their own line, which reasoning models emit constantly). Each becomes its own
 * step, exactly as the model divided it. No markers ⇒ one untitled segment: the
 * alternative would be inventing a summary, which is the one thing a reasoning trace
 * must never do. */
export function splitThought(text: string): { title: string | null; body: string }[] {
  const out: { title: string | null; body: string }[] = [];
  let current: { title: string | null; body: string } = { title: null, body: "" };
  for (const line of text.split("\n")) {
    const heading = line.trim().match(/^(?:\*\*(.+?)\*\*|#{1,6}\s+(.+?))\s*:?\s*$/);
    if (heading) {
      if (current.title || current.body.trim()) out.push(current);
      current = { title: (heading[1] || heading[2]).trim(), body: "" };
    } else {
      current.body += (current.body ? "\n" : "") + line;
    }
  }
  if (current.title || current.body.trim()) out.push(current);
  return out.length ? out : [{ title: null, body: text }];
}

/** Systems whose live tools are named `<prefix>_<verb>_<noun>`. */
const SYSTEMS: Record<string, string> = {
  ado: "Azure DevOps", jira: "Jira", confluence: "Confluence", github: "GitHub",
  gitlab: "GitLab", octopus: "Octopus", onedrive: "OneDrive", grafana: "Grafana",
  datadog: "Datadog", dynatrace: "Dynatrace", elastic: "Elasticsearch", web: "the web",
};

/** Hand-written phrasing for the built-in tools — the ones that carry most of a run.
 * Connector tools fall through to the derivation below, which keeps this table from
 * growing to the ~60 live tools the connectors register. */
const LABELS: Record<string, string> = {
  search_memory: "Searching learned memory",
  remember: "Saving to memory",
  list_sources: "Listing connected systems",
  list_gaps: "Reviewing knowledge gaps",
  graph_neighbors: "Exploring connections",
  graph_relations: "Reading the knowledge graph",
  graph_path: "Tracing a connection",
  scrape_website: "Reading a website",
  list_connector_types: "Listing connector types",
  add_connector: "Adding a connector",
  sync_source: "Syncing a connector",
  qj_api: "Calling the QuickJoiner API",
  qj_api_reference: "Checking the API reference",
};

const humanize = (s: string) => s.replace(/_/g, " ").trim();

/** A readable title for any tool, including ones this file has never heard of. */
export function actionLabel(tool: string): string {
  if (LABELS[tool]) return LABELS[tool];
  const [prefix, ...rest] = tool.split("_");
  const system = SYSTEMS[prefix];
  if (!system || !rest.length) return humanize(tool).replace(/^./, (c) => c.toUpperCase());
  const [verb, ...noun] = rest;
  const what = humanize(noun.join(" "));
  if (verb === "search") return `Searching ${system}`;
  if (verb === "list") return what ? `Listing ${what} in ${system}` : `Listing ${system}`;
  if (verb === "get" || verb === "read") return what ? `Reading ${what} in ${system}` : `Reading ${system}`;
  return `${system}: ${humanize(rest.join(" "))}`;
}

/** Argument keys worth showing on the collapsed line, best first — the thing the
 * step was actually about. */
const DETAIL_KEYS = [
  "query", "question", "q", "fact", "url", "entity", "src", "path", "rel",
  "name", "repo", "project", "branch", "id", "wiql", "file_path",
];

/** The one-line detail: the most meaningful argument, or a compact key=value list
 * when nothing matches (a tool this file doesn't know still reads sensibly). */
export function actionDetail(tool: string, args: Record<string, string>): string {
  const entries = Object.entries(args || {}).filter(([, v]) => v !== "" && v != null);
  if (!entries.length) return "";
  if (tool === "qj_api") {
    const method = args.method || "GET";
    return `${String(method).toUpperCase()} ${args.path || ""}`.trim();
  }
  if (tool === "graph_path" && args.src && args.dst) return `${args.src} → ${args.dst}`;
  if (tool === "graph_relations" && args.rel) {
    // The relation alone reads as a bare "works_on"; the types are what make the step
    // legible as "people → teams", and they are the arguments most often supplied.
    const ends = [args.src_type, args.dst_type].filter(Boolean);
    return ends.length === 2 ? `${args.rel} · ${ends[0]} → ${ends[1]}` : args.rel;
  }
  for (const key of DETAIL_KEYS) {
    if (args[key]) return args[key];
  }
  return entries.map(([k, v]) => `${k}: ${v}`).join(" · ");
}

function ActionIcon({ tool }: { tool: string }) {
  const p = { size: 13, className: "text-accent" };
  if (tool === "search_memory") return <Search {...p} />;
  if (tool === "remember") return <Database {...p} />;
  if (tool === "graph_relations") return <Network {...p} />;
  if (tool === "graph_neighbors") return <Waypoints {...p} />;
  if (tool === "graph_path") return <Route {...p} />;
  if (tool === "list_sources" || tool === "list_connector_types") return <Boxes {...p} />;
  if (tool === "add_connector" || tool === "sync_source") return <Plug {...p} />;
  if (tool.startsWith("qj_api")) return <Terminal {...p} />;
  if (tool === "scrape_website" || tool.startsWith("web_")) return <Globe {...p} />;
  if (tool.includes("search")) return <Search {...p} />;
  if (tool.includes("_get") || tool.includes("_read") || tool.includes("file")) return <FileText {...p} />;
  if (tool.startsWith("gitlab_") || tool.startsWith("github_") || tool.startsWith("ado_")) return <GitBranch {...p} />;
  return <Layers {...p} />;
}

/** One row: rail node, title, and a body that clamps to a line until expanded.
 * The chevron is the expansion indicator and only renders when there is genuinely
 * more to see, so it never promises detail a step doesn't have. */
function Row({
  icon, title, preview, detail, running, failed, last,
}: {
  icon: React.ReactNode;
  title: string;
  preview: string;
  detail?: React.ReactNode;
  running?: boolean;
  failed?: boolean;
  last?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const expandable = Boolean(detail);
  return (
    <div className="relative flex gap-3 pb-3.5">
      {/* rail: the connecting line, stopping at the final node */}
      {/* `hair`, not `border`: at 0.07 alpha the hairline token is invisible against the
          panel fill, and a timeline whose connecting line can't be seen is just a list. */}
      {!last && <span className="absolute bottom-0 left-[11px] top-[22px] w-px bg-hair" aria-hidden />}
      <span className="relative z-[1] mt-[2px] flex h-[22px] w-[22px] flex-shrink-0 items-center justify-center rounded-full bg-fill2">
        {running ? <Loader2 size={12} className="animate-spin text-accent" /> : icon}
      </span>
      <div className="min-w-0 flex-1">
        <button
          type="button"
          onClick={() => expandable && setOpen((v) => !v)}
          disabled={!expandable}
          aria-expanded={expandable ? open : undefined}
          className={cn(
            "flex w-full items-center gap-1.5 text-left",
            expandable && "cursor-pointer",
          )}
        >
          <span className={cn("truncate text-[12.5px] font-semibold", failed ? "text-gold" : "text-ink")}>
            {title}
          </span>
          {failed && <TriangleAlert size={11} className="flex-shrink-0 text-gold" />}
          {expandable && (
            <ChevronDown
              size={13}
              className={cn("flex-shrink-0 text-faint transition-transform", open && "rotate-180")}
            />
          )}
        </button>
        {open && detail ? (
          <div className="mt-1 text-[12.5px] leading-[1.65] text-muted">{detail}</div>
        ) : (
          preview && (
            <div className="mt-0.5 truncate text-[12.5px] leading-[1.65] text-muted">{preview}</div>
          )
        )}
      </div>
    </div>
  );
}

function ActionRow({ step, last }: { step: Extract<TraceStep, { kind: "action" }>; last?: boolean }) {
  const detailLine = actionDetail(step.tool, step.args);
  const res = step.result;
  const args = Object.entries(step.args || {});
  const detail = (
    <div className="space-y-2">
      {args.length > 0 && (
        <div className="space-y-0.5">
          {args.map(([k, v]) => (
            <div key={k} className="flex gap-2 font-mono text-[11.5px]">
              <span className="flex-shrink-0 text-faint">{k}</span>
              <span className="min-w-0 whitespace-pre-wrap break-words text-muted">{v}</span>
            </div>
          ))}
        </div>
      )}
      {res && (
        <div>
          <div className="mb-1 flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.12em] text-faint">
            <span>{res.ok ? "result" : "error"}</span>
            {/* The preview is bounded; state the real size rather than implying this is all of it. */}
            {res.chars > res.summary.length && (
              <span className="normal-case tracking-normal">
                showing first {res.summary.length} of {res.chars.toLocaleString()} chars
              </span>
            )}
          </div>
          <pre className="max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words rounded-sm bg-fill2 px-2.5 py-2 font-mono text-[11px] leading-normal text-muted">
            {res.summary || "(empty)"}
          </pre>
        </div>
      )}
      <div className="font-mono text-[10.5px] text-faint">{step.tool}</div>
    </div>
  );
  return (
    <Row
      icon={<ActionIcon tool={step.tool} />}
      title={actionLabel(step.tool)}
      preview={detailLine}
      detail={detail}
      running={!res}
      failed={res ? !res.ok : false}
      last={last}
    />
  );
}

/** How many steps a trace holds once thoughts are split on their own headings —
 * what the collapsed header counts. */
export function stepCount(trace: TraceStep[]): number {
  return trace.reduce(
    (n, s) =>
      n + (s.kind === "thought"
        ? splitThought(s.text).filter((x) => x.title || x.body.trim()).length
        : 1),
    0,
  );
}

export function ThinkingTrace({ trace, streaming }: { trace: TraceStep[]; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  // Follow the run live, then get out of the way: expanded while streaming, collapsed
  // once the answer lands (the answer is the point; the trace is the audit trail).
  const expanded = streaming ? !open : open;
  if (!trace.length) return null;

  // Flatten to rows here rather than in state: deltas arrive mid-word, so a heading
  // is only reliably a heading once the line is complete.
  const rows: React.ReactNode[] = [];
  const flat: { node: (last: boolean) => React.ReactNode }[] = [];
  for (const step of trace) {
    if (step.kind === "action") {
      const s = step;
      flat.push({ node: (last) => <ActionRow key={`a-${s.id}-${flat.length}`} step={s} last={last} /> });
    } else if (step.kind === "note") {
      const key = `n-${flat.length}`;
      const text = step.text;
      flat.push({
        node: (last) => (
          <Row key={key} icon={<Globe size={13} className="text-accent" />} title={text} preview="" last={last} />
        ),
      });
    } else {
      for (const seg of splitThought(step.text)) {
        if (!seg.title && !seg.body.trim()) continue;
        const key = `t-${flat.length}`;
        const body = seg.body.trim();
        flat.push({
          node: (last) => (
            <Row
              key={key}
              icon={<Sparkles size={13} className="text-accent" />}
              title={seg.title || "Reasoning"}
              preview={body}
              detail={body ? <div className="whitespace-pre-wrap break-words">{body}</div> : undefined}
              last={last}
            />
          ),
        });
      }
    }
  }
  flat.forEach((f, i) => rows.push(f.node(i === flat.length - 1)));

  return (
    <div className="mb-3 overflow-hidden rounded-sm bg-fill">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3.5 py-2 text-left transition hover:text-ink"
      >
        <span className={cn("text-[11px] font-semibold uppercase tracking-[0.14em] text-muted", streaming && "animate-pulse2")}>
          {streaming ? "QuickJoiner is thinking…" : "Reasoning trace"}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {rows.length} step{rows.length === 1 ? "" : "s"}
        </span>
        <ChevronDown
          size={14}
          className={cn("ml-auto flex-shrink-0 text-faint transition-transform", expanded && "rotate-180")}
        />
      </button>
      {expanded && (
        <div className="max-h-[420px] overflow-y-auto px-3.5 pb-1 pt-1">{rows}</div>
      )}
    </div>
  );
}
