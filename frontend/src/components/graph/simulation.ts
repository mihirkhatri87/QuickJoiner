/* Live force layout for the knowledge graph.
 *
 * The previous layout was a one-shot: 300 synchronous d3 ticks, then a frozen
 * snapshot of coordinates handed to React. Every merge ("expand neighbors",
 * a path result) re-ran it and nodes teleported to their new coordinates. The
 * simulation here is *persistent* instead — it keeps node objects (and their
 * velocities) alive across data changes, so a merge reheats an existing
 * layout and the graph visibly makes room for what arrived, and a node the
 * user drags pushes its neighbourhood around while the rest settles back.
 *
 * The render loop owns the ticking (one tick per animation frame while hot);
 * this module never starts d3's internal timer, so layout motion and camera
 * motion stay on the same clock and can be stopped together.
 */

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
} from "d3-force";
import type { GraphData, GraphEdge } from "../../types";

export interface SimNode {
  id: string;
  name: string;
  type: string;
  /** Deterministic architectural layer of the defining file, or '' / undefined when the
   * path stated nothing (the majority — see quickjoiner/ingest/layers.py). */
  layer?: string;
  /** Live layout position — mutated in place by d3 every tick. */
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Set while the user drags this node; cleared on release. */
  fx?: number | null;
  fy?: number | null;
  /** Number of edges touching this node in the current data — the importance
   * signal for both mark size and level-of-detail selection. */
  degree: number;
}

export interface SimLink {
  edge: GraphEdge;
  source: SimNode;
  target: SimNode;
}

/** Below this alpha the layout is treated as settled: the render loop stops
 * ticking (and can stop entirely, so an idle graph costs no frames). */
export const ALPHA_MIN = 0.015;

/** Radius used for collision and, scaled, for the painted mark. */
export function nodeRadius(degree: number): number {
  return 5 + Math.min(degree, 6) * 1.3;
}

export class GraphLayout {
  nodes: SimNode[] = [];
  links: SimLink[] = [];
  byId = new Map<string, SimNode>();

  private sim: Simulation<SimNode, undefined>;

  constructor() {
    this.sim = forceSimulation<SimNode>([])
      .alphaDecay(0.032)
      .velocityDecay(0.36)
      .stop();
  }

  /** Replace the graph contents, carrying over the position AND velocity of
   * every node that survives. New nodes are seeded on top of an already-placed
   * neighbour (with a little jitter) rather than at the origin or at a random
   * far corner, so an expansion grows outward from the thing that was
   * expanded instead of raining in from off-screen. */
  setData(data: GraphData): { added: Set<string> } {
    const added = new Set<string>();
    const nextById = new Map<string, SimNode>();
    const nodes: SimNode[] = [];

    for (const n of data.nodes) {
      const prev = this.byId.get(n.id);
      if (prev) {
        prev.name = n.name;
        prev.type = n.type;
        prev.layer = n.layer;
        prev.degree = 0;
        nodes.push(prev);
        nextById.set(n.id, prev);
      } else {
        const fresh: SimNode = { ...n, x: NaN, y: NaN, vx: 0, vy: 0, degree: 0 };
        nodes.push(fresh);
        nextById.set(n.id, fresh);
        added.add(n.id);
      }
    }

    const links: SimLink[] = [];
    for (const e of data.edges) {
      const source = nextById.get(e.src);
      const target = nextById.get(e.dst);
      if (!source || !target) continue;
      source.degree += 1;
      target.degree += 1;
      links.push({ edge: e, source, target });
    }

    if (added.size) this.seedNewNodes(nodes, links, added);

    this.nodes = nodes;
    this.links = links;
    this.byId = nextById;

    this.sim.nodes(nodes);
    this.sim
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          // Denser hubs get a little more room, so a high-degree node doesn't
          // bury its own label under a ring of neighbours.
          .distance((l) => 58 + Math.min(46, (l.source.degree + l.target.degree) * 1.6))
          .strength(0.32),
      )
      .force("charge", forceManyBody<SimNode>().strength(-210).distanceMax(720))
      .force("collide", forceCollide<SimNode>((d) => nodeRadius(d.degree) + 9).strength(0.85))
      // Weak centring instead of forceCenter: this graph is usually many
      // disconnected components, and forceCenter translates the *whole* system
      // every tick (everything drifts under the cursor). Independent x/y
      // springs just stop components sailing off forever.
      .force("x", forceX<SimNode>(0).strength(0.035))
      .force("y", forceY<SimNode>(0).strength(0.035));

    return { added };
  }

  private seedNewNodes(nodes: SimNode[], links: SimLink[], added: Set<string>): void {
    const anchors = new Map<string, SimNode>();
    for (const l of links) {
      if (added.has(l.source.id) && !added.has(l.target.id) && Number.isFinite(l.target.x)) {
        if (!anchors.has(l.source.id)) anchors.set(l.source.id, l.target);
      }
      if (added.has(l.target.id) && !added.has(l.source.id) && Number.isFinite(l.source.x)) {
        if (!anchors.has(l.target.id)) anchors.set(l.target.id, l.source);
      }
    }
    const placed = nodes.filter((n) => Number.isFinite(n.x));
    const cx = placed.length ? placed.reduce((s, n) => s + n.x, 0) / placed.length : 0;
    const cy = placed.length ? placed.reduce((s, n) => s + n.y, 0) / placed.length : 0;
    let i = 0;
    for (const n of nodes) {
      if (!added.has(n.id)) continue;
      const anchor = anchors.get(n.id);
      // Golden-angle spiral: deterministic, and it spaces siblings evenly
      // instead of clumping them the way uniform random does.
      const angle = i * 2.399963;
      const spread = anchor ? 26 : 140 + Math.sqrt(i) * 26;
      const origin = anchor ?? { x: cx, y: cy };
      n.x = origin.x + Math.cos(angle) * spread;
      n.y = origin.y + Math.sin(angle) * spread;
      n.vx = 0;
      n.vy = 0;
      i += 1;
    }
  }

  /** Advance one frame. */
  tick(): void {
    this.sim.tick();
  }

  /** Settle synchronously — used once, for the very first snapshot, so the
   * initial view opens on a readable layout instead of a big bang. */
  warmup(ticks: number): void {
    for (let i = 0; i < ticks; i += 1) this.sim.tick();
  }

  hot(): boolean {
    return this.sim.alpha() > ALPHA_MIN;
  }

  alpha(): number {
    return this.sim.alpha();
  }

  /** Stir the layout back to life (a merge, or a node being dragged). */
  reheat(alpha: number): void {
    this.sim.alpha(Math.max(this.sim.alpha(), alpha));
  }

  cool(): void {
    this.sim.alpha(0);
  }
}
