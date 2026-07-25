/* Level of detail — the "map tiles" layer of the graph canvas.
 *
 * A knowledge graph grows without bound as the user expands neighbourhoods,
 * but a browser will not draw (or hit-test, or re-layout) tens of thousands of
 * SVG elements at 60fps. A map solves this by never drawing the whole world:
 * it draws the tiles you are looking at, at the detail your zoom warrants, and
 * it re-tiles only when you stop moving. The same three rules apply here:
 *
 *   1. CULL — only entities inside the visible world rect (plus a margin, so
 *      a small pan reveals content that is already drawn) are rendered.
 *   2. BUDGET — if that is still too many, keep the most important ones
 *      (whatever the user has selected or pathed to, then by degree). The
 *      count of what was dropped is returned so the UI can *say so* rather
 *      than silently pretending the graph is smaller than it is.
 *   3. RE-TILE ON REST — `lodKey` quantises the viewport, so panning and
 *      zooming reuse the existing render set (the world group is one transform
 *      write) and the set is only recomputed when the camera has genuinely
 *      moved to a new "tile", or when the layout itself has changed.
 *
 * Pure: takes live layout nodes, returns the subset to render.
 */

import type { SimLink, SimNode } from "./simulation";

export interface WorldRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LodBudget {
  nodes: number;
  edges: number;
}

export const DEFAULT_BUDGET: LodBudget = { nodes: 1400, edges: 2200 };

/** How far beyond the viewport to keep drawing, as a fraction of the viewport
 * size on each side. Half a screen of overscan means an ordinary drag never
 * outruns the render set before the next re-tile lands. */
export const OVERSCAN = 0.5;

export interface LodResult {
  nodes: SimNode[];
  links: SimLink[];
  /** Totals in the full graph, so the toolbar can report "N of M drawn". */
  totalNodes: number;
  totalLinks: number;
  /** True when the budget (not the viewport) forced entities out of the set —
   * i.e. there is detail here that zooming in will reveal. */
  budgetLimited: boolean;
}

export function overscanRect(view: WorldRect, overscan = OVERSCAN): WorldRect {
  const px = view.w * overscan;
  const py = view.h * overscan;
  return { x: view.x - px, y: view.y - py, w: view.w + px * 2, h: view.h + py * 2 };
}

function inside(r: WorldRect, n: SimNode): boolean {
  return n.x >= r.x && n.x <= r.x + r.w && n.y >= r.y && n.y <= r.y + r.h;
}

/** Quantised viewport identity. Two cameras with the same key are close
 * enough that they can share a render set; a change means "re-tile". */
export function lodKey(view: WorldRect): string {
  const step = Math.max(1, view.w / 6);
  const band = Math.round(Math.log2(Math.max(1, view.w)) * 3);
  return `${Math.round(view.x / step)}:${Math.round(view.y / step)}:${band}`;
}

/**
 * @param pinned ids that must always render regardless of viewport or budget —
 *   the selected node, its neighbours, the nodes on a highlighted path. Losing
 *   those to a cull would break the interaction the user is in the middle of.
 */
export function selectVisible(
  nodes: readonly SimNode[],
  links: readonly SimLink[],
  view: WorldRect,
  pinned: ReadonlySet<string>,
  budget: LodBudget = DEFAULT_BUDGET,
): LodResult {
  const rect = overscanRect(view);
  let candidates: SimNode[] = [];
  for (const n of nodes) {
    if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) continue;
    if (pinned.has(n.id) || inside(rect, n)) candidates.push(n);
  }

  let budgetLimited = false;
  if (candidates.length > budget.nodes) {
    budgetLimited = true;
    // Pinned first, then the best-connected: at a wide zoom the structural
    // backbone is what carries meaning, and the long tail of degree-1 leaves
    // is what the user zooms in to read.
    candidates = candidates
      .slice()
      .sort((a, b) => {
        const pa = pinned.has(a.id) ? 1 : 0;
        const pb = pinned.has(b.id) ? 1 : 0;
        if (pa !== pb) return pb - pa;
        if (a.degree !== b.degree) return b.degree - a.degree;
        return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
      })
      .slice(0, budget.nodes);
  }

  const kept = new Set(candidates.map((n) => n.id));
  // Both endpoints must be drawn: an edge to a node that was culled would
  // read as a relationship pointing at nothing.
  let visibleLinks = links.filter((l) => kept.has(l.source.id) && kept.has(l.target.id));
  if (visibleLinks.length > budget.edges) {
    budgetLimited = true;
    visibleLinks = visibleLinks
      .slice()
      .sort((a, b) => {
        const pa = pinned.has(a.source.id) || pinned.has(a.target.id) ? 1 : 0;
        const pb = pinned.has(b.source.id) || pinned.has(b.target.id) ? 1 : 0;
        if (pa !== pb) return pb - pa;
        return b.source.degree + b.target.degree - (a.source.degree + a.target.degree);
      })
      .slice(0, budget.edges);
  }

  return {
    nodes: candidates,
    links: visibleLinks,
    totalNodes: nodes.length,
    totalLinks: links.length,
    budgetLimited,
  };
}
