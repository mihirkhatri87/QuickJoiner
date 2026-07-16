/* Knowledge graph view: force-directed layout over /api/graph. Every
 * entity-selecting action (search suggestion, suggested-starting-point chip,
 * node double-click, path-finder result) uses the same verb — "expand": fetch
 * that entity's neighborhood and merge it into the current view rather than
 * replacing it, so exploring reads as a guided walk instead of a sequence of
 * unrelated snapshots. Existing node positions are carried into the next
 * layout pass (only newly-added nodes get fresh coordinates), so the graph
 * settles rather than jumping. Bridge entities (touched by more than one
 * source — the actual cross-source correlation) get a persistent halo and are
 * surfaced as suggested starting points instead of leaving the user to find
 * them in an arbitrary graph slice. The path finder is graph_path made
 * interactive: pick two entities, see the connecting chain highlighted with a
 * flowing dash and evidence per hop — a question-answer interaction rather
 * than a diagram to parse. */

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import {
  ArrowRight,
  CircleAlert,
  Expand,
  Filter,
  Maximize2,
  Minus,
  Plus,
  RefreshCw,
  Route,
  Scan,
  Search,
  Shrink,
  Sparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Markdown } from "./markdown";
import type { BridgeEntity, DocumentFile, EntitySearchResult, GraphData, GraphEdge, GraphNode } from "../types";

// Grouped categorical scheme: three validated hue families (see index.css) plus
// a neutral bucket. 11 distinct entity types can't each carry a safe, distinct
// hue (the design system has 3 non-status categorical hues), so types fold into
// the axis a user actually cares about — text (name + type badge, already shown
// on hover/click) carries the fine-grained identity; color carries the family.
const TYPE_GROUP: Record<string, string> = {
  repo: "code", project: "code", symbol: "code", module: "code",
  package: "dep",
  service: "org", environment: "org", ticket: "org", team: "org", person: "org",
};
const GROUP_COLOR: Record<string, string> = {
  code: "var(--graph-code)",
  dep: "var(--graph-dep)",
  org: "var(--graph-org)",
};
const color = (type: string) => GROUP_COLOR[TYPE_GROUP[type] ?? ""] ?? "var(--faint)";
const GROUP_LABEL: Record<string, string> = { code: "Code", dep: "Dependencies", org: "Org & ops" };

function edgeKey(e: GraphEdge): string {
  return `${e.src}|${e.rel}|${e.dst}|${e.evidence.doc_id}`;
}

interface LayoutNode extends GraphNode {
  x: number;
  y: number;
  degree: number;
}

interface Laid {
  nodes: LayoutNode[];
  edges: (GraphEdge & { x1: number; y1: number; x2: number; y2: number })[];
}

function layout(data: GraphData, previous?: Map<string, { x: number; y: number }>): Laid {
  const hadPrevious = !!previous?.size;
  const nodes = data.nodes.map((n) => {
    const prev = previous?.get(n.id);
    return { ...n, degree: 0, ...(prev ? { x: prev.x, y: prev.y } : {}) };
  }) as (LayoutNode & { vx?: number; vy?: number })[];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = data.edges
    .filter((e) => byId.has(e.src) && byId.has(e.dst))
    .map((e) => ({ ...e, source: e.src, target: e.dst }));
  for (const l of links) {
    byId.get(l.src)!.degree += 1;
    byId.get(l.dst)!.degree += 1;
  }
  const sim = forceSimulation(nodes)
    .force("link", forceLink(links).id((d) => (d as LayoutNode).id).distance(75).strength(0.6))
    .force("charge", forceManyBody().strength(-200))
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide(20))
    .stop();
  // A view that's just growing (expand/path-find) only needs new nodes to
  // settle near their neighbors — a short re-tick avoids re-shuffling
  // everything the user has already found and oriented themselves around.
  const ticks = hadPrevious ? 120 : 300;
  for (let i = 0; i < ticks; i += 1) sim.tick();
  return {
    nodes: nodes as LayoutNode[],
    edges: links.map((l) => {
      const s = l.source as unknown as LayoutNode;
      const t = l.target as unknown as LayoutNode;
      return { src: l.src, rel: l.rel, dst: l.dst, detail: l.detail, evidence: l.evidence,
               x1: s.x, y1: s.y, x2: t.x, y2: t.y };
    }),
  };
}

function mergeGraphData(base: GraphData | null, incoming: GraphData): { merged: GraphData; addedIds: Set<string> } {
  const nodeMap = new Map((base?.nodes ?? []).map((n) => [n.id, n]));
  const addedIds = new Set<string>();
  for (const n of incoming.nodes) {
    if (!nodeMap.has(n.id)) {
      nodeMap.set(n.id, n);
      addedIds.add(n.id);
    }
  }
  const edgeMap = new Map((base?.edges ?? []).map((e) => [edgeKey(e), e]));
  for (const e of incoming.edges) {
    const k = edgeKey(e);
    if (!edgeMap.has(k)) edgeMap.set(k, e);
  }
  return { merged: { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] }, addedIds };
}

const EVIDENCE_CHIP_CLASS =
  "inline-block max-w-full truncate rounded-[5px] border border-gold-line bg-gold-soft px-1.5 py-px font-mono text-[9.5px] text-gold";

// Whether an evidence URI is shaped like something a local-file lookup could
// actually resolve — mirrors connectors/local_view.py's own two supported
// cases exactly: a git clone's "<remote_url>::<relative_path>" convention, or
// a local files/ source's file:// URI. This is deliberately NOT based on
// evidence.kind: a repo's own README/AGENTS.md/ADRs are ingested as kind="doc"
// (only real source extensions get kind="code" — see files.py), but they sit
// in the exact same git clone as the code and should get the same local view.
// Confluence/Jira/GitHub-API citations never look like either shape, so they
// fall through to the original plain-link behavior untouched.
function looksLocal(uri: string | null): boolean {
  return !!uri && (uri.includes("::") || uri.startsWith("file://"));
}

function EvidenceChip({
  evidence,
  onOpenFile,
}: {
  evidence: GraphEdge["evidence"];
  onOpenFile: (file: DocumentFile) => void;
}) {
  const label = evidence.title || evidence.uri || evidence.doc_id;
  const isUrl = !!evidence.uri && /^https?:\/\//.test(evidence.uri);
  const [busy, setBusy] = useState(false);

  // Everything that isn't shaped like a local file (Confluence, Jira, taught
  // notes, GitHub/GitLab-API citations, ...) keeps the exact original
  // behavior: a plain <a target="_blank">, so native middle-click / ctrl-click
  // / right-click-open-in-new-tab / hover-preview all keep working — a JS
  // onClick handler would quietly break every one of those.
  if (!looksLocal(evidence.uri)) {
    return isUrl ? (
      <a href={evidence.uri!} target="_blank" rel="noopener noreferrer" title={evidence.uri!}
         className="block hover:opacity-75">
        <span className={EVIDENCE_CHIP_CLASS}>{label}</span>
      </a>
    ) : (
      <span title={evidence.uri || undefined} className="block">
        <span className={EVIDENCE_CHIP_CLASS}>{label}</span>
      </span>
    );
  }

  // Locally-shaped evidence: the file is very likely sitting in a local clone
  // already (git connector) or a local files/ source — try that first, and
  // only fall back to the remote link if the backend can't actually find it
  // (e.g. the clone was removed, or the URI shape was a false positive).
  const handleClick = async () => {
    setBusy(true);
    try {
      const file = await api.documentFile(evidence.doc_id);
      onOpenFile(file);
    } catch {
      if (isUrl) window.open(evidence.uri!, "_blank", "noopener,noreferrer");
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={busy}
      title={evidence.uri || undefined}
      className={EVIDENCE_CHIP_CLASS + " text-left hover:brightness-110 disabled:opacity-60"}
    >
      {label}
    </button>
  );
}

function TypeBadge({ type }: { type: string }) {
  return (
    <span
      className="inline-block rounded-full px-2 py-px font-mono text-[9px] uppercase tracking-[0.14em]"
      style={{ color: color(type), background: "var(--fill)" }}
    >
      {type}
    </span>
  );
}

function NodePanel({
  node,
  edges,
  names,
  bridgeInfo,
  onClose,
  onFocus,
  onExpand,
  onOpenFile,
}: {
  node: GraphNode;
  edges: GraphEdge[];
  names: Map<string, string>;
  bridgeInfo?: BridgeEntity;
  onClose: () => void;
  onFocus: (id: string) => void;
  onExpand: (id: string) => void;
  onOpenFile: (file: DocumentFile) => void;
}) {
  const touching = edges.filter((e) => e.src === node.id || e.dst === node.id);
  return (
    <aside className="scroll-thin w-[290px] flex-shrink-0 overflow-y-auto border-l border-fill2 bg-panel px-4 py-4 backdrop-blur-xl">
      <div className="mb-1 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-[14.5px] font-semibold text-ink">{node.name}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <TypeBadge type={node.type} />
            {bridgeInfo && bridgeInfo.source_count > 1 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 py-px font-mono text-[9px] uppercase tracking-[0.12em] text-accent">
                <Sparkles size={9} /> {bridgeInfo.source_count} sources
              </span>
            )}
          </div>
        </div>
        <button onClick={onClose} aria-label="Close panel"
                className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-fill text-muted hover:text-ink">
          <X size={13} />
        </button>
      </div>
      <button
        onClick={() => onExpand(node.id)}
        className="mt-2 flex items-center gap-1.5 rounded-full bg-fill px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink hover:bg-fill2"
      >
        <Maximize2 size={11} /> Expand neighbors
      </button>
      <div className="mt-3 flex flex-col gap-2.5">
        {touching.length === 0 && (
          <p className="text-[12px] text-muted">No recorded relationships.</p>
        )}
        {touching.map((e, i) => {
          const outgoing = e.src === node.id;
          const farId = outgoing ? e.dst : e.src;
          return (
            <div key={i} className="rounded-sm bg-fill px-3 py-2">
              <div className="font-mono text-[10px] text-muted">
                {outgoing ? `${e.rel} →` : `← ${e.rel}`}
              </div>
              <button
                onClick={() => onFocus(farId)}
                className="mt-0.5 break-words text-left text-[12.5px] font-medium text-ink hover:text-accent"
              >
                {names.get(farId) ?? farId}
              </button>
              {e.detail && <div className="mt-0.5 text-[11px] leading-snug text-muted">{e.detail}</div>}
              <div className="mt-1.5">
                <EvidenceChip evidence={e.evidence} onOpenFile={onOpenFile} />
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

/** Hoisted to module scope deliberately: this used to be defined inline inside
 * PathPanel's render body, which meant React saw a brand-new component type
 * on every PathPanel re-render (e.g. every debounced suggestions update) and
 * unmounted/remounted the underlying <input> each time — dropping keyboard
 * focus on every keystroke, which is exactly what made typing here feel
 * unresponsive. A stable top-level component identity fixes that; suggestions
 * stay a controlled prop (the parent owns sugA/sugB), but dismissal (click
 * outside) is handled locally so a stale dropdown doesn't linger. */
function EntityPicker({
  value, onChange, suggestions, onPick, onDismiss, placeholder,
}: {
  value: string; onChange: (v: string) => void; suggestions: EntitySearchResult[];
  onPick: (r: EntitySearchResult) => void; onDismiss: () => void; placeholder: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!suggestions.length) return;
    const onDocPointerDown = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) onDismiss();
    };
    document.addEventListener("pointerdown", onDocPointerDown);
    return () => document.removeEventListener("pointerdown", onDocPointerDown);
  }, [suggestions.length, onDismiss]);

  return (
    <div className="relative flex-1" ref={wrapRef}>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-full bg-fill px-3 py-1.5 text-[12.5px] text-ink outline-none placeholder:text-faint"
      />
      {suggestions.length > 0 && (
        <div className="scroll-thin absolute left-0 right-0 top-[calc(100%+4px)] z-20 max-h-48 overflow-y-auto rounded-lg bg-raised2 py-1 shadow-lg">
          {suggestions.map((s) => (
            <button
              key={s.id}
              onMouseDown={() => { onPick(s); onChange(s.name); }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-fill"
            >
              <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full" style={{ background: color(s.type) }} />
              <span className="truncate text-[12px] text-ink">{s.name}</span>
              <span className="ml-auto font-mono text-[9px] uppercase text-faint">{s.type}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function PathPanel({
  onClose,
  onSubmit,
  result,
  error,
  busy,
  names,
}: {
  onClose: () => void;
  onSubmit: (a: string, b: string) => void;
  result: { hops: GraphEdge[] } | null;
  error: string | null;
  busy: boolean;
  names: Map<string, string>;
}) {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [sugA, setSugA] = useState<EntitySearchResult[]>([]);
  const [sugB, setSugB] = useState<EntitySearchResult[]>([]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (a.trim().length >= 2) api.searchEntities(a.trim()).then(setSugA).catch(() => setSugA([]));
      else setSugA([]);
    }, 200);
    return () => clearTimeout(t);
  }, [a]);
  useEffect(() => {
    const t = setTimeout(() => {
      if (b.trim().length >= 2) api.searchEntities(b.trim()).then(setSugB).catch(() => setSugB([]));
      else setSugB([]);
    }, 200);
    return () => clearTimeout(t);
  }, [b]);

  return (
    <div className="mx-5 mb-2 flex flex-col gap-2 rounded-lg bg-panel px-4 py-3 backdrop-blur-xl md:mx-8">
      <div className="flex items-center gap-2">
        <Route size={13} className="text-accent" />
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-muted">
          How are two entities related?
        </span>
        <button onClick={onClose} className="ml-auto text-faint hover:text-ink"><X size={13} /></button>
      </div>
      <form
        className="flex items-center gap-2"
        onSubmit={(e) => { e.preventDefault(); if (a.trim() && b.trim()) onSubmit(a.trim(), b.trim()); }}
      >
        <EntityPicker value={a} onChange={setA} suggestions={sugA} onPick={() => setSugA([])}
                      onDismiss={() => setSugA([])} placeholder="First entity…" />
        <ArrowRight size={13} className="flex-shrink-0 text-faint" />
        <EntityPicker value={b} onChange={setB} suggestions={sugB} onPick={() => setSugB([])}
                      onDismiss={() => setSugB([])} placeholder="Second entity…" />
        <button
          type="submit"
          disabled={busy || !a.trim() || !b.trim()}
          className="flex-shrink-0 rounded-full bg-accent-soft px-3.5 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-accent hover:brightness-125 disabled:opacity-40"
        >
          {busy ? "Finding…" : "Find path"}
        </button>
      </form>
      {error && <div className="font-mono text-[11px] text-danger">{error}</div>}
      {result && (
        <div className="scroll-thin flex flex-wrap items-center gap-1.5 overflow-x-auto pt-1">
          {result.hops.map((h, i) => (
            <span key={i} className="flex items-center gap-1.5">
              {i === 0 && (
                <span className="rounded-full bg-fill px-2 py-1 text-[12px] font-medium text-ink">
                  {names.get(h.src) ?? h.src}
                </span>
              )}
              <span className="font-mono text-[10px] text-muted">
                --{h.rel}--&gt;
              </span>
              <span className="rounded-full bg-fill px-2 py-1 text-[12px] font-medium text-ink">
                {names.get(h.dst) ?? h.dst}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// Extension -> shiki language id. Covers the ecosystems this codebase's own
// connectors already parse (deps.py) plus common source/config file types;
// anything unrecognized falls back to "text" (no highlighting, still renders).
const EXT_LANG: Record<string, string> = {
  ".cs": "csharp", ".vb": "vb", ".fs": "fsharp",
  ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
  ".py": "python", ".pyi": "python", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
  ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
  ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
  ".csproj": "xml", ".vbproj": "xml", ".fsproj": "xml", ".props": "xml", ".targets": "xml", ".config": "xml",
  ".nuspec": "xml", ".html": "html", ".css": "css", ".sql": "sql",
  ".sh": "bash", ".bash": "bash", ".ps1": "powershell", ".psm1": "powershell",
  ".md": "markdown", ".gradle": "groovy", ".tf": "hcl", ".ini": "ini", ".properties": "properties",
};
function langForPath(path: string): string {
  const base = path.toLowerCase().split(/[/\\]/).pop() || path.toLowerCase();
  if (base.startsWith("dockerfile")) return "docker";
  if (base === "makefile") return "makefile";
  const dot = base.lastIndexOf(".");
  return (dot >= 0 && EXT_LANG[base.slice(dot)]) || "text";
}

function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

/** Local-file viewer for evidence chips whose citation resolves to a real file
 * on disk (a git clone or a local files/ source) — a closable overlay, not a
 * new page: it never touches graph state (selection, viewport, filters), so
 * closing it returns to exactly the graph view as it was when the node/chip
 * was clicked. Shiki is dynamically imported (same lazy-load pattern already
 * used for mermaid elsewhere in this app) so its cost is paid only on first
 * use, not on every graph-view load.
 *
 * Markdown files (READMEs, ADRs, ...) get a second view: this app's own
 * dependency-free <Markdown> renderer (headings/lists/tables/mermaid — the
 * same component ArtifactModal uses), toggled against the raw Shiki-
 * highlighted source. Rendered is the default — reading a formatted README
 * beats staring at literal #/** markup — with Source always one click away. */
function FileViewModal({ file, onClose }: { file: DocumentFile | null; onClose: () => void }) {
  const [size, setSize] = useState<"docked" | "full">("docked");
  const [html, setHtml] = useState<string | null>(null);
  const [view, setView] = useState<"rendered" | "source">("rendered");
  const markdown = !!file && isMarkdownPath(file.path);

  useEffect(() => {
    setView(file && isMarkdownPath(file.path) ? "rendered" : "source");
  }, [file]);

  useEffect(() => {
    if (!file) return;
    setHtml(null);
    let cancelled = false;
    (async () => {
      try {
        const { codeToHtml } = await import("shiki");
        const rendered = await codeToHtml(file.text, {
          lang: langForPath(file.path),
          themes: { light: "github-light", dark: "github-dark" },
        });
        if (!cancelled) setHtml(rendered);
      } catch {
        if (!cancelled) {
          const escaped = file.text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!));
          setHtml(`<pre class="shiki"><code>${escaped}</code></pre>`);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [file]);

  useEffect(() => {
    if (!file) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [file, onClose]);

  if (!file) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className={
          "flex flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl transition-all " +
          (size === "full" ? "h-full w-full" : "h-[70vh] w-[70vw]")
        }
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <span className="h-[8px] w-[8px] flex-shrink-0 rounded-full" style={{ background: "var(--graph-code)" }} />
          <div className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink">{file.path}</div>
          {markdown && (
            <div className="flex flex-shrink-0 overflow-hidden rounded-full bg-fill font-mono text-[10px] uppercase tracking-[0.1em]">
              <button
                onClick={() => setView("rendered")}
                className={"px-2.5 py-1 " + (view === "rendered" ? "bg-accent-soft text-accent" : "text-muted hover:text-ink")}
              >
                Preview
              </button>
              <button
                onClick={() => setView("source")}
                className={"px-2.5 py-1 " + (view === "source" ? "bg-accent-soft text-accent" : "text-muted hover:text-ink")}
              >
                Source
              </button>
            </div>
          )}
          <button
            onClick={() => setSize((s) => (s === "full" ? "docked" : "full"))}
            title={size === "full" ? "Restore" : "Fullscreen"}
            className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-fill text-muted hover:text-ink"
          >
            {size === "full" ? <Shrink size={13} /> : <Expand size={13} />}
          </button>
          <button
            onClick={onClose}
            aria-label="Close"
            className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-fill text-muted hover:text-ink"
          >
            <X size={13} />
          </button>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-auto p-4 text-[12.5px] leading-relaxed">
          {markdown && view === "rendered" ? (
            <Markdown text={file.text} mermaid />
          ) : html ? (
            <div dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <div className="font-mono text-[11.5px] text-muted">Loading…</div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Isolated so typing/searching never touches GraphView's own state — with a
 * few hundred graph nodes in the canvas, routing every keystroke through the
 * parent's state would re-run that entire render tree on each character,
 * which is exactly what made the search box feel unresponsive on a large
 * graph. Also handles its own dismissal: a dropdown that only closes on
 * Escape or a successful pick stays open forever if you click elsewhere. */
function SearchBox({ onSelect }: { onSelect: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<EntitySearchResult[]>([]);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (query.trim().length < 2) {
      setSuggestions([]);
      return;
    }
    const t = setTimeout(() => {
      api.searchEntities(query.trim(), 8).then(setSuggestions).catch(() => setSuggestions([]));
    }, 220);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    if (!suggestions.length) return;
    const onDocPointerDown = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setSuggestions([]);
    };
    document.addEventListener("pointerdown", onDocPointerDown);
    return () => document.removeEventListener("pointerdown", onDocPointerDown);
  }, [suggestions.length]);

  const pick = (id: string) => {
    onSelect(id);
    setQuery("");
    setSuggestions([]);
  };

  return (
    <div className="relative" ref={wrapRef}>
      <form
        className="flex items-center gap-1.5 rounded-full bg-fill px-3 py-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (suggestions[0]) pick(suggestions[0].id);
        }}
      >
        <Search size={13} className="text-faint" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") { setQuery(""); setSuggestions([]); } }}
          placeholder="Find an entity or alias…"
          className="w-[190px] bg-transparent text-[12.5px] text-ink outline-none placeholder:text-faint"
        />
      </form>
      {suggestions.length > 0 && (
        <div className="scroll-thin absolute left-0 right-0 top-[calc(100%+6px)] z-20 max-h-56 overflow-y-auto rounded-lg bg-raised2 py-1 shadow-lg">
          {suggestions.map((s) => (
            <button
              key={s.id}
              onMouseDown={() => pick(s.id)}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-fill"
            >
              <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full" style={{ background: color(s.type) }} />
              <span className="truncate text-[12.5px] text-ink">{s.name}</span>
              <span className="ml-auto font-mono text-[9px] uppercase text-faint">{s.type}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function GraphView() {
  const [data, setData] = useState<GraphData | null>(null);
  const [laid, setLaid] = useState<Laid | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [fileView, setFileView] = useState<DocumentFile | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const [bridges, setBridges] = useState<BridgeEntity[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [vb, setVb] = useState({ x: -480, y: -320, w: 960, h: 640 });
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [hiddenRels, setHiddenRels] = useState<Set<string>>(new Set());
  const [showFilters, setShowFilters] = useState(false);
  const [pathMode, setPathMode] = useState(false);
  const [pathEdgeKeys, setPathEdgeKeys] = useState<Set<string>>(new Set());
  const [pathHops, setPathHops] = useState<GraphEdge[] | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [pathBusy, setPathBusy] = useState(false);
  const [newIds, setNewIds] = useState<Set<string>>(new Set());
  const [ripple, setRipple] = useState<{ id: string; key: number } | null>(null);
  const dragRef = useRef<{ px: number; py: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const minimapRef = useRef<SVGSVGElement>(null);
  const minimapDragging = useRef(false);
  const bridgeMap = useMemo(() => new Map(bridges.map((b) => [b.id, b])), [bridges]);

  // A force layout with many disconnected components (common once edges are
  // fairly sampled across dozens of source entities instead of one dominant
  // hub) can spread far past any fixed viewBox — auto-fitting to whatever is
  // actually laid out, every time, is what keeps the whole graph on screen
  // instead of "3 dots at the edge of an empty canvas."
  const fitBounds = useCallback((pts: { x: number; y: number }[], pad = 140) => {
    if (!pts.length) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
    const w = Math.max(320, maxX - minX), h = Math.max(240, maxY - minY);
    setVb({ x: (minX + maxX) / 2 - w / 2, y: (minY + maxY) / 2 - h / 2, w, h });
  }, []);

  const relayout = useCallback((next: GraphData, added?: Set<string>, focusIds?: string[]) => {
    const previous = new Map((laid?.nodes ?? []).map((n) => [n.id, { x: n.x, y: n.y }]));
    const nextLaid = layout(next, previous);
    setData(next);
    setLaid(nextLaid);
    // Zoom to what was just searched/expanded — its neighborhood, not the whole
    // (possibly hundreds-strong) merged graph, so the thing you looked for is
    // actually the thing that ends up on screen, not one speck among many.
    const focusSet = focusIds?.length ? new Set(focusIds) : null;
    const target = focusSet ? nextLaid.nodes.filter((n) => focusSet.has(n.id)) : nextLaid.nodes;
    fitBounds(target.length ? target : nextLaid.nodes, 160);
    if (added && added.size) {
      setNewIds(added);
      setTimeout(() => setNewIds(new Set()), 450);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [laid, fitBounds]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.graph(undefined, 400);
      const laidOut = layout(d);
      setData(d);
      setLaid(laidOut);
      fitBounds(laidOut.nodes, 160);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [fitBounds]);

  const expand = useCallback(
    async (entity: string, opts: { focus?: boolean } = {}) => {
      try {
        const d = await api.graph(entity, 200);
        const { merged, addedIds } = mergeGraphData(data, d);
        relayout(merged, addedIds, d.nodes.map((n) => n.id));
        if (opts.focus !== false) setSelected(d.entity?.id ?? entity);
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [data, relayout],
  );

  useEffect(() => {
    load();
    api.graphBridges(16).then(setBridges).catch(() => setBridges([]));
  }, [load]);

  const names = useMemo(
    () => new Map((data?.nodes ?? []).map((n) => [n.id, n.name])),
    [data],
  );
  const selectedNode = data?.nodes.find((n) => n.id === selected) ?? null;
  const relTypes = useMemo(() => [...new Set((data?.edges ?? []).map((e) => e.rel))].sort(), [data]);
  const groupsPresent = useMemo(
    () => [...new Set((data?.nodes ?? []).map((n) => TYPE_GROUP[n.type] ?? "other"))],
    [data],
  );

  const visibleNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const n of data?.nodes ?? []) {
      if (!hiddenTypes.has(n.type)) ids.add(n.id);
    }
    return ids;
  }, [data, hiddenTypes]);
  const visibleEdges = useMemo(
    () =>
      (laid?.edges ?? []).filter(
        (e) => !hiddenRels.has(e.rel) && visibleNodeIds.has(e.src) && visibleNodeIds.has(e.dst),
      ),
    [laid, hiddenRels, visibleNodeIds],
  );
  const visibleNodes = useMemo(
    () => (laid?.nodes ?? []).filter((n) => visibleNodeIds.has(n.id)),
    [laid, visibleNodeIds],
  );

  const neighborIds = useMemo(() => {
    if (!selected || !data) return new Set<string>();
    const ids = new Set<string>([selected]);
    for (const e of data.edges) {
      if (e.src === selected) ids.add(e.dst);
      if (e.dst === selected) ids.add(e.src);
    }
    return ids;
  }, [selected, data]);

  const fitToIds = useCallback(
    (ids: string[], nodes: LayoutNode[]) => fitBounds(nodes.filter((n) => ids.includes(n.id)), 140),
    [fitBounds],
  );

  const runPathFind = useCallback(
    async (a: string, b: string) => {
      setPathBusy(true);
      setPathError(null);
      setPathHops(null);
      try {
        const res = await api.graphPath(a, b, 5);
        if (!res.path) {
          setPathError("No recorded chain within 5 hops — not learned, not invented.");
          return;
        }
        if (res.path.length === 0) {
          setPathError("That's the same entity.");
          return;
        }
        const pathNodeIds = new Set<string>([res.a.id, res.b.id]);
        for (const h of res.path) { pathNodeIds.add(h.src); pathNodeIds.add(h.dst); }
        const pathNodes: GraphNode[] = [res.a, res.b];
        const known = new Set((data?.nodes ?? []).map((n) => n.id));
        for (const h of res.path) {
          if (!known.has(h.src)) pathNodes.push({ id: h.src, name: names.get(h.src) ?? h.src, type: "service" });
          if (!known.has(h.dst)) pathNodes.push({ id: h.dst, name: names.get(h.dst) ?? h.dst, type: "service" });
        }
        const { merged, addedIds } = mergeGraphData(data, { nodes: pathNodes, edges: res.path });
        const nextLaid = layout(merged, new Map((laid?.nodes ?? []).map((n) => [n.id, { x: n.x, y: n.y }])));
        setData(merged);
        setLaid(nextLaid);
        if (addedIds.size) {
          setNewIds(addedIds);
          setTimeout(() => setNewIds(new Set()), 450);
        }
        setPathEdgeKeys(new Set(res.path.map(edgeKey)));
        setPathHops(res.path);
        fitToIds([...pathNodeIds], nextLaid.nodes);
      } catch (e) {
        setPathError(String(e instanceof Error ? e.message : e));
      } finally {
        setPathBusy(false);
      }
    },
    [data, laid, names, fitToIds],
  );

  const zoomBy = useCallback((factor: number) => {
    setVb((v) => {
      const w = Math.min(4000, Math.max(200, v.w * factor));
      const h = w * (v.h / v.w);
      return { x: v.x + (v.w - w) / 2, y: v.y + (v.h - h) / 2, w, h };
    });
  }, []);
  // Trackpad pinch is delivered to the browser as a `wheel` event with
  // ctrlKey=true (there's no separate cross-browser pinch/gesture event for
  // this) — same as a plain scroll-wheel tick otherwise. React's onWheel prop
  // is attached passively, so calling preventDefault() there is silently
  // ignored and the browser zooms the whole page underneath the canvas as
  // well as (or instead of) the graph. A native, explicitly non-passive
  // listener is the only way to actually absorb the gesture here.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onNativeWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomBy(e.deltaY > 0 ? 1.15 : 1 / 1.15);
    };
    el.addEventListener("wheel", onNativeWheel, { passive: false });
    return () => el.removeEventListener("wheel", onNativeWheel);
  }, [zoomBy]);
  const onPointerDown = (e: React.PointerEvent) => {
    dragRef.current = { px: e.clientX, py: e.clientY };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag || !svgRef.current) return;
    // Capture deltas NOW: the setVb updater runs at render time, after
    // pointerup may have nulled the ref — it must not touch dragRef.
    const width = svgRef.current.getBoundingClientRect().width;
    const dxPx = e.clientX - drag.px;
    const dyPx = e.clientY - drag.py;
    dragRef.current = { px: e.clientX, py: e.clientY };
    setVb((v) => {
      const scale = v.w / width;
      return { ...v, x: v.x - dxPx * scale, y: v.y - dyPx * scale };
    });
  };
  const onPointerUp = () => {
    dragRef.current = null;
  };

  // Overview/minimap: the full extent of every currently-loaded node (not just
  // the ones passing the type/relation filters — the map should always show
  // where everything is, even what's temporarily hidden), padded so the
  // outermost nodes aren't flush against the minimap's edge.
  const MINIMAP_W = 168, MINIMAP_H = 112;
  const fullExtent = useMemo(() => {
    const pts = laid?.nodes ?? [];
    if (!pts.length) return { x: -400, y: -300, w: 800, h: 600 };
    const pad = 60;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
    return { x: minX, y: minY, w: Math.max(200, maxX - minX), h: Math.max(200, maxY - minY) };
  }, [laid]);

  const panMinimapTo = useCallback(
    (clientX: number, clientY: number) => {
      const rect = minimapRef.current?.getBoundingClientRect();
      if (!rect) return;
      const scaleX = fullExtent.w / rect.width, scaleY = fullExtent.h / rect.height;
      const worldX = fullExtent.x + (clientX - rect.left) * scaleX;
      const worldY = fullExtent.y + (clientY - rect.top) * scaleY;
      setVb((v) => ({ ...v, x: worldX - v.w / 2, y: worldY - v.h / 2 }));
    },
    [fullExtent],
  );
  const onMinimapPointerDown = (e: React.PointerEvent) => {
    minimapDragging.current = true;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    panMinimapTo(e.clientX, e.clientY);
  };
  const onMinimapPointerMove = (e: React.PointerEvent) => {
    if (minimapDragging.current) panMinimapTo(e.clientX, e.clientY);
  };
  const onMinimapPointerUp = () => {
    minimapDragging.current = false;
  };

  const toggleType = (t: string) =>
    setHiddenTypes((s) => {
      const next = new Set(s);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  const toggleRel = (r: string) =>
    setHiddenRels((s) => {
      const next = new Set(s);
      next.has(r) ? next.delete(r) : next.add(r);
      return next;
    });

  return (
    <div className="flex min-h-0 flex-1">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* toolbar */}
        <div className="flex flex-wrap items-center gap-2 px-5 pb-2 pt-1 md:px-8">
          <SearchBox key={pathMode ? "path" : "browse"} onSelect={expand} />
          <button
            onClick={() => setPathMode((v) => !v)}
            className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] ${
              pathMode ? "bg-accent-soft text-accent" : "bg-fill text-muted hover:text-ink"
            }`}
          >
            <Route size={12} /> Path finder
          </button>
          <button
            onClick={() => setShowFilters((v) => !v)}
            className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] ${
              showFilters ? "bg-accent-soft text-accent" : "bg-fill text-muted hover:text-ink"
            }`}
          >
            <Filter size={12} /> Filters
          </button>
          {(hiddenTypes.size > 0 || hiddenRels.size > 0) && (
            <button
              onClick={() => { setHiddenTypes(new Set()); setHiddenRels(new Set()); }}
              className="rounded-full bg-fill px-2.5 py-1.5 font-mono text-[10px] uppercase text-faint hover:text-ink"
            >
              Clear filters
            </button>
          )}
          <button
            onClick={load}
            title="Reset to overview"
            className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted hover:text-ink"
          >
            <RefreshCw size={13} />
          </button>
          <span className="ml-auto font-mono text-[10.5px] uppercase tracking-[0.14em] text-faint">
            {data ? `${visibleNodes.length}/${data.nodes.length} entities · ${visibleEdges.length}/${data.edges.length} relationships` : ""}
          </span>
        </div>

        {showFilters && (
          <div className="mx-5 mb-2 flex flex-wrap items-center gap-3 rounded-lg bg-panel px-4 py-2.5 backdrop-blur-xl md:mx-8">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 font-mono text-[9.5px] uppercase text-faint">Types</span>
              {groupsPresent.map((g) => (
                <span key={g} className="font-mono text-[9.5px] uppercase text-faint">
                  {GROUP_LABEL[g] ?? g}
                </span>
              ))}
              {[...new Set((data?.nodes ?? []).map((n) => n.type))].sort().map((t) => (
                <button
                  key={t}
                  onClick={() => toggleType(t)}
                  className="flex items-center gap-1 rounded-full px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.1em]"
                  style={{
                    background: hiddenTypes.has(t) ? "var(--fill)" : "var(--fill-2)",
                    color: hiddenTypes.has(t) ? "var(--faint)" : "var(--text)",
                    textDecoration: hiddenTypes.has(t) ? "line-through" : "none",
                    opacity: hiddenTypes.has(t) ? 0.5 : 1,
                  }}
                >
                  <span className="h-2 w-2 rounded-full" style={{ background: color(t) }} />
                  {t}
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-1.5 border-l border-fill2 pl-3">
              <span className="mr-1 font-mono text-[9.5px] uppercase text-faint">Relations</span>
              {relTypes.map((r) => (
                <button
                  key={r}
                  onClick={() => toggleRel(r)}
                  className="rounded-full px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.1em]"
                  style={{
                    background: hiddenRels.has(r) ? "var(--fill)" : "var(--fill-2)",
                    color: hiddenRels.has(r) ? "var(--faint)" : "var(--text)",
                    textDecoration: hiddenRels.has(r) ? "line-through" : "none",
                    opacity: hiddenRels.has(r) ? 0.5 : 1,
                  }}
                >
                  {r}
                </button>
              ))}
            </div>
          </div>
        )}

        {pathMode && (
          <PathPanel
            onClose={() => { setPathMode(false); setPathHops(null); setPathEdgeKeys(new Set()); setPathError(null); }}
            onSubmit={runPathFind}
            result={pathHops ? { hops: pathHops } : null}
            error={pathError}
            busy={pathBusy}
            names={names}
          />
        )}

        {bridges.length > 0 && !pathMode && (
          <div className="mx-5 mb-2 flex flex-wrap items-center gap-1.5 md:mx-8">
            <span className="flex items-center gap-1 font-mono text-[9.5px] uppercase tracking-[0.12em] text-faint">
              <Sparkles size={10} className="text-accent" /> Where sources connect:
            </span>
            {bridges.slice(0, 10).map((b) => (
              <button
                key={b.id}
                onClick={() => expand(b.id)}
                className="flex items-center gap-1.5 rounded-full bg-fill px-2.5 py-1 text-[11.5px] text-ink hover:bg-fill2"
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: color(b.type) }} />
                {b.name}
                <span className="font-mono text-[9px] text-accent">{b.source_count}×</span>
              </button>
            ))}
          </div>
        )}

        {/* canvas */}
        <div className="relative mx-5 mb-5 min-h-0 flex-1 overflow-hidden rounded-lg bg-fill md:mx-8">
          {error && (
            <div className="absolute inset-x-0 top-3 z-10 mx-auto w-fit rounded-full bg-danger-soft px-4 py-1.5 font-mono text-[11px] text-danger">
              <CircleAlert size={12} className="mr-1.5 inline" />
              {error}
            </div>
          )}
          {!loading && laid && laid.nodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center px-8 text-center text-[13.5px] leading-relaxed text-muted">
              Nothing in the knowledge graph yet. Sync a repo with dependency manifests,
              connect Jira/Octopus, or teach facts that mention ticket keys — relationships
              show up here as they are learned.
            </div>
          )}
          <svg
            ref={svgRef}
            className="h-full w-full cursor-grab touch-none active:cursor-grabbing"
            viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onClick={() => setSelected(null)}
          >
            {visibleEdges.map((e, i) => {
              const active = !selected || e.src === selected || e.dst === selected;
              const onPath = pathEdgeKeys.has(edgeKey(e));
              return (
                <line
                  key={i}
                  x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                  stroke={onPath ? "var(--accent)" : "var(--muted)"}
                  strokeOpacity={onPath ? 0.9 : active ? 0.45 : 0.12}
                  strokeWidth={onPath ? 2.4 : active && selected ? 1.6 : 1}
                  className={onPath ? "graph-edge-flow" : undefined}
                  style={{ transition: "stroke-opacity 200ms ease, stroke-width 200ms ease" }}
                />
              );
            })}
            {visibleNodes.map((n) => {
              const dim = selected != null && !neighborIds.has(n.id) && !pathEdgeKeys.size;
              const r = 5 + Math.min(n.degree, 6) * 1.3;
              const showAllLabels = visibleNodes.length <= 90;
              const label = showAllLabels || n.id === hovered || neighborIds.has(n.id) || pathEdgeKeys.size > 0;
              const bridge = bridgeMap.get(n.id);
              const isNew = newIds.has(n.id);
              return (
                <g
                  key={n.id}
                  transform={`translate(${n.x},${n.y})`}
                  opacity={dim ? 0.25 : 1}
                  style={{ transition: "opacity 200ms ease" }}
                >
                  <g className={isNew ? "graph-node-enter" : undefined}>
                    <g
                      className="graph-node-inner cursor-pointer"
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelected(n.id === selected ? null : n.id);
                      }}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        setRipple({ id: n.id, key: Date.now() });
                        expand(n.id, { focus: false });
                      }}
                      onPointerEnter={(e) => {
                        setHovered(n.id);
                        const rect = svgRef.current?.getBoundingClientRect();
                        if (rect) setHoverPos({ x: e.clientX - rect.left, y: e.clientY - rect.top });
                      }}
                      onPointerMove={(e) => {
                        const rect = svgRef.current?.getBoundingClientRect();
                        if (rect) setHoverPos({ x: e.clientX - rect.left, y: e.clientY - rect.top });
                      }}
                      onPointerLeave={() => { setHovered(null); setHoverPos(null); }}
                    >
                      {bridge && bridge.source_count > 1 && (
                        <circle r={r + 5} fill="none" stroke={color(n.type)} strokeWidth={1.5}
                                className="graph-bridge-halo" />
                      )}
                      <circle r={r} fill={color(n.type)} fillOpacity={0.9}
                              stroke={n.id === selected ? "var(--text)" : "transparent"} strokeWidth={1.5} />
                      {/* transparent hit target — bigger than the painted mark, per interaction guidance */}
                      <circle r={Math.max(r, 13)} fill="transparent" />
                      {ripple?.id === n.id && (
                        <circle
                          key={ripple.key}
                          r={r}
                          fill="none"
                          stroke={color(n.type)}
                          strokeWidth={2}
                          className="graph-click-ripple"
                          onAnimationEnd={() => setRipple(null)}
                        />
                      )}
                    </g>
                  </g>
                  {label && (
                    <text
                      y={r + 11}
                      textAnchor="middle"
                      style={{ fill: "var(--muted)", fontSize: 9.5, fontFamily: "Geist Mono, monospace" }}
                    >
                      {n.name.length > 30 ? n.name.slice(0, 29) + "…" : n.name}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
          {/* hover tooltip — screen-space, follows the pointer independent of the viewBox transform */}
          {hovered && hoverPos && (() => {
            const n = data?.nodes.find((x) => x.id === hovered);
            if (!n) return null;
            const b = bridgeMap.get(n.id);
            const deg = laid?.nodes.find((x) => x.id === n.id)?.degree ?? 0;
            return (
              <div
                className="pointer-events-none absolute z-20 rounded-md bg-raised2 px-2.5 py-1.5 text-[11.5px] shadow-lg"
                style={{ left: hoverPos.x + 14, top: hoverPos.y + 14, maxWidth: 220 }}
              >
                <div className="truncate font-semibold text-ink">{n.name}</div>
                <div className="mt-0.5 flex items-center gap-1.5">
                  <TypeBadge type={n.type} />
                  <span className="font-mono text-[9.5px] text-faint">{deg} links</span>
                </div>
                {b && b.source_count > 1 && (
                  <div className="mt-0.5 flex items-center gap-1 font-mono text-[9.5px] text-accent">
                    <Sparkles size={9} /> spans {b.source_count} sources
                  </div>
                )}
              </div>
            );
          })()}
          {/* legend */}
          {groupsPresent.length > 0 && (
            <div className="absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-full bg-panel px-3.5 py-1.5 backdrop-blur-xl">
              {groupsPresent.map((g) => (
                <span key={g} className="flex items-center gap-1.5 font-mono text-[9.5px] uppercase tracking-[0.12em] text-muted">
                  <span className="h-2 w-2 rounded-full" style={{ background: GROUP_COLOR[g] ?? "var(--faint)" }} />
                  {GROUP_LABEL[g] ?? g}
                </span>
              ))}
              <span className="flex items-center gap-1.5 font-mono text-[9.5px] uppercase tracking-[0.12em] text-muted">
                <Sparkles size={9} className="text-accent" /> bridges sources
              </span>
            </div>
          )}
          {/* overview minimap + zoom controls */}
          {laid && laid.nodes.length > 0 && (
            <div className="absolute bottom-3 right-3 flex flex-col items-end gap-2">
              <div className="overflow-hidden rounded-md bg-panel p-1 backdrop-blur-xl">
                <svg
                  ref={minimapRef}
                  width={MINIMAP_W}
                  height={MINIMAP_H}
                  viewBox={`${fullExtent.x} ${fullExtent.y} ${fullExtent.w} ${fullExtent.h}`}
                  className="cursor-pointer touch-none rounded-[3px]"
                  style={{ background: "var(--fill)" }}
                  onPointerDown={onMinimapPointerDown}
                  onPointerMove={onMinimapPointerMove}
                  onPointerUp={onMinimapPointerUp}
                  onPointerLeave={onMinimapPointerUp}
                >
                  {laid.nodes.map((n) => (
                    <circle key={n.id} cx={n.x} cy={n.y} r={Math.max(fullExtent.w, fullExtent.h) / 140}
                            fill={color(n.type)} fillOpacity={0.75} />
                  ))}
                  <rect
                    x={vb.x} y={vb.y} width={vb.w} height={vb.h}
                    fill="var(--accent)" fillOpacity={0.08}
                    stroke="var(--accent)" strokeWidth={Math.max(fullExtent.w, fullExtent.h) / 220}
                  />
                </svg>
              </div>
              <div className="flex flex-col overflow-hidden rounded-full bg-panel backdrop-blur-xl">
                <button
                  onClick={() => zoomBy(1 / 1.3)}
                  title="Zoom in"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Plus size={14} />
                </button>
                <div className="h-px bg-fill2" />
                <button
                  onClick={() => zoomBy(1.3)}
                  title="Zoom out"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Minus size={14} />
                </button>
                <div className="h-px bg-fill2" />
                <button
                  onClick={() => fitBounds(laid.nodes, 160)}
                  title="Fit everything in view"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Scan size={13} />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
      {selectedNode && data && (
        <NodePanel
          node={selectedNode}
          edges={data.edges}
          names={names}
          bridgeInfo={bridgeMap.get(selectedNode.id)}
          onClose={() => setSelected(null)}
          onFocus={(id) => setSelected(id)}
          onExpand={(id) => expand(id, { focus: false })}
          onOpenFile={setFileView}
        />
      )}
      <FileViewModal file={fileView} onClose={() => setFileView(null)} />
    </div>
  );
}
