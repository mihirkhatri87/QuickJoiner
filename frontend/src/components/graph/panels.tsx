/* Side panels and overlays for the knowledge-graph view: the evidence chip and
 * node inspector, the path finder, the entity search box, and the local-file
 * viewer. Split out of GraphView.tsx when the canvas itself became a render
 * loop — these are ordinary React, they re-render on interaction, and keeping
 * them beside the 60fps canvas code made both harder to read. Behaviour is
 * unchanged from when they lived there. */

import { ArrowRight, Expand, Maximize2, Route, Search, Shrink, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { Markdown } from "../markdown";
import { color } from "./theme";
import type { BridgeEntity, DocumentFile, EntitySearchResult, GraphEdge, GraphNode } from "../../types";

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

export function TypeBadge({ type }: { type: string }) {
  return (
    <span
      className="inline-block rounded-full px-2 py-px font-mono text-[9px] uppercase tracking-[0.14em]"
      style={{ color: color(type), background: "var(--fill)" }}
    >
      {type}
    </span>
  );
}

export function NodePanel({
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
    // Floats OVER the canvas rather than sitting beside it: as a flex sibling
    // it shrank the canvas every time a node was selected, so the whole graph
    // lurched sideways on click — which also stole the second click of a
    // double-click (the node had moved out from under the pointer).
    <aside className="scroll-thin absolute bottom-2 right-2 top-2 z-30 w-[290px] overflow-y-auto rounded-lg bg-panel px-4 py-4 shadow-panel backdrop-blur-xl">
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

export function PathPanel({
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
export function FileViewModal({ file, onClose }: { file: DocumentFile | null; onClose: () => void }) {
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
export function SearchBox({ onSelect }: { onSelect: (id: string) => void }) {
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
