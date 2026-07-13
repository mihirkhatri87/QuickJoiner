/* Knowledge graph view: force-directed layout over /api/graph. Nodes are
 * colored by entity type (CSS tokens, so both themes work), clicking a node
 * opens an evidence panel, and the search box resolves org aliases
 * ("nautical models") server-side. The layout runs to completion synchronously
 * (a few hundred ticks) — snapshots are capped, so this stays instant. */

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import { CircleAlert, RefreshCw, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { GraphData, GraphEdge, GraphNode } from "../types";

const TYPE_COLOR: Record<string, string> = {
  repo: "var(--accent)",
  package: "var(--gold)",
  project: "var(--accent-hi)",
  ticket: "var(--unknown)",
  service: "var(--danger)",
  environment: "var(--muted)",
  source: "var(--faint)",
};
const color = (type: string) => TYPE_COLOR[type] ?? "var(--faint)";

interface LayoutNode extends GraphNode {
  x: number;
  y: number;
  degree: number;
}

interface Laid {
  nodes: LayoutNode[];
  edges: (GraphEdge & { x1: number; y1: number; x2: number; y2: number })[];
}

function layout(data: GraphData): Laid {
  const nodes = data.nodes.map((n) => ({ ...n, degree: 0 })) as (LayoutNode & {
    vx?: number;
    vy?: number;
  })[];
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
  for (let i = 0; i < 300; i += 1) sim.tick();
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

function EvidenceChip({ evidence }: { evidence: GraphEdge["evidence"] }) {
  const label = evidence.title || evidence.uri || evidence.doc_id;
  const isUrl = !!evidence.uri && /^https?:\/\//.test(evidence.uri);
  const chip = (
    <span className="inline-block max-w-full truncate rounded-[5px] border border-gold-line bg-gold-soft px-1.5 py-px font-mono text-[9.5px] text-gold">
      {label}
    </span>
  );
  return isUrl ? (
    <a href={evidence.uri!} target="_blank" rel="noopener noreferrer" title={evidence.uri!}
       className="block hover:opacity-75">
      {chip}
    </a>
  ) : (
    <span title={evidence.uri || undefined} className="block">{chip}</span>
  );
}

function NodePanel({
  node,
  edges,
  names,
  onClose,
  onFocus,
}: {
  node: GraphNode;
  edges: GraphEdge[];
  names: Map<string, string>;
  onClose: () => void;
  onFocus: (id: string) => void;
}) {
  const touching = edges.filter((e) => e.src === node.id || e.dst === node.id);
  return (
    <aside className="scroll-thin w-[290px] flex-shrink-0 overflow-y-auto border-l border-fill2 bg-panel px-4 py-4 backdrop-blur-xl">
      <div className="mb-1 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-[14.5px] font-semibold text-ink">{node.name}</div>
          <span
            className="mt-1 inline-block rounded-full px-2 py-px font-mono text-[9px] uppercase tracking-[0.14em]"
            style={{ color: color(node.type), background: "var(--fill)" }}
          >
            {node.type}
          </span>
        </div>
        <button onClick={onClose} aria-label="Close panel"
                className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-fill text-muted hover:text-ink">
          <X size={13} />
        </button>
      </div>
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
                <EvidenceChip evidence={e.evidence} />
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

export function GraphView() {
  const [data, setData] = useState<GraphData | null>(null);
  const [laid, setLaid] = useState<Laid | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [focused, setFocused] = useState<string | null>(null); // server-side entity focus
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [vb, setVb] = useState({ x: -480, y: -320, w: 960, h: 640 });
  const dragRef = useRef<{ px: number; py: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const load = useCallback(async (entity?: string) => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.graph(entity);
      setData(d);
      setLaid(layout(d));
      setFocused(entity ?? null);
      setSelected(d.entity?.id ?? null);
      setVb({ x: -480, y: -320, w: 960, h: 640 });
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const names = useMemo(
    () => new Map((data?.nodes ?? []).map((n) => [n.id, n.name])),
    [data],
  );
  const selectedNode = data?.nodes.find((n) => n.id === selected) ?? null;
  const showAllLabels = (laid?.nodes.length ?? 0) <= 90;
  const neighborIds = useMemo(() => {
    if (!selected || !data) return new Set<string>();
    const ids = new Set<string>([selected]);
    for (const e of data.edges) {
      if (e.src === selected) ids.add(e.dst);
      if (e.dst === selected) ids.add(e.src);
    }
    return ids;
  }, [selected, data]);

  const onWheel = (e: React.WheelEvent) => {
    const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    setVb((v) => {
      const w = Math.min(4000, Math.max(200, v.w * factor));
      const h = w * (v.h / v.w);
      return { x: v.x + (v.w - w) / 2, y: v.y + (v.h - h) / 2, w, h };
    });
  };
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

  const legendTypes = useMemo(
    () => [...new Set((data?.nodes ?? []).map((n) => n.type))],
    [data],
  );

  return (
    <div className="flex min-h-0 flex-1">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* toolbar */}
        <div className="flex flex-wrap items-center gap-2 px-5 pb-2 pt-1 md:px-8">
          <form
            className="flex items-center gap-1.5 rounded-full bg-fill px-3 py-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (query.trim()) load(query.trim());
            }}
          >
            <Search size={13} className="text-faint" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Find an entity or alias…"
              className="w-[190px] bg-transparent text-[12.5px] text-ink outline-none placeholder:text-faint"
            />
          </form>
          {focused && (
            <button
              onClick={() => {
                setQuery("");
                load();
              }}
              className="flex items-center gap-1.5 rounded-full bg-accent-soft px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-accent hover:brightness-125"
            >
              <X size={12} /> Focused: {names.get(data?.entity?.id ?? "") ?? focused} — show all
            </button>
          )}
          <button
            onClick={() => load(focused ?? undefined)}
            title="Refresh"
            className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted hover:text-ink"
          >
            <RefreshCw size={13} />
          </button>
          <span className="ml-auto font-mono text-[10.5px] uppercase tracking-[0.14em] text-faint">
            {data ? `${data.nodes.length} entities · ${data.edges.length} relationships` : ""}
          </span>
        </div>

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
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onClick={() => setSelected(null)}
          >
            {laid?.edges.map((e, i) => {
              const active = !selected || e.src === selected || e.dst === selected;
              return (
                <line
                  key={i}
                  x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                  stroke="var(--muted)"
                  strokeOpacity={active ? 0.45 : 0.12}
                  strokeWidth={active && selected ? 1.6 : 1}
                />
              );
            })}
            {laid?.nodes.map((n) => {
              const dim = selected != null && !neighborIds.has(n.id);
              const r = 5 + Math.min(n.degree, 6) * 1.3;
              const label = showAllLabels || n.id === hovered || neighborIds.has(n.id);
              return (
                <g
                  key={n.id}
                  transform={`translate(${n.x},${n.y})`}
                  opacity={dim ? 0.25 : 1}
                  className="cursor-pointer"
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelected(n.id === selected ? null : n.id);
                  }}
                  onPointerEnter={() => setHovered(n.id)}
                  onPointerLeave={() => setHovered(null)}
                >
                  <circle r={r} fill={color(n.type)} fillOpacity={0.9}
                          stroke={n.id === selected ? "var(--ink)" : "transparent"} strokeWidth={1.5} />
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
          {/* legend */}
          {legendTypes.length > 0 && (
            <div className="absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-full bg-panel px-3.5 py-1.5 backdrop-blur-xl">
              {legendTypes.map((t) => (
                <span key={t} className="flex items-center gap-1.5 font-mono text-[9.5px] uppercase tracking-[0.12em] text-muted">
                  <span className="h-2 w-2 rounded-full" style={{ background: color(t) }} />
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      {selectedNode && data && (
        <NodePanel
          node={selectedNode}
          edges={data.edges}
          names={names}
          onClose={() => setSelected(null)}
          onFocus={(id) => setSelected(id)}
        />
      )}
    </div>
  );
}
