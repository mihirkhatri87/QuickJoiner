/* Knowledge graph canvas.
 *
 * Every entity-selecting action (search suggestion, suggested-starting-point
 * chip, node double-click, path-finder result) uses the same verb — "expand":
 * fetch that entity's neighborhood and merge it into the current view rather
 * than replacing it, so exploring reads as a guided walk instead of a sequence
 * of unrelated snapshots. Bridge entities (touched by more than one source —
 * the actual cross-source correlation) get a persistent halo and are surfaced
 * as suggested starting points. The path finder is graph_path made
 * interactive: pick two entities, see the connecting chain highlighted with a
 * flowing dash and evidence per hop.
 *
 * The canvas is built on three layers, each in ./graph:
 *
 *   camera.ts     — a continuous {x,y,k} camera that a render loop *eases*
 *                   toward a target, with pointer-throw inertia. Panning
 *                   glides and coasts; zooming is anchored on the cursor and
 *                   settles instead of stepping. `markScale` keeps a node's
 *                   painted size near-constant on screen at any zoom, which is
 *                   what stops a wide auto-fit rendering the graph as dust.
 *   simulation.ts — a *persistent* force layout. Merges reheat the existing
 *                   layout (new nodes are seeded on their anchor neighbour) so
 *                   the graph makes room for what arrived instead of
 *                   teleporting, and nodes can be dragged live.
 *   lod.ts        — map-tile style level of detail: cull to the visible world
 *                   rect plus overscan, cap the drawn set by importance, and
 *                   re-tile only when the camera lands on a new tile. Panning
 *                   and zooming are then a single transform write, not a
 *                   re-render of thousands of elements.
 *
 * Consequently React owns *what* exists (which nodes/edges/labels, selection,
 * filters) and the render loop owns *where* it is drawn: node transforms and
 * edge endpoints are written imperatively and never appear in JSX, so a
 * re-render can never fight the animation.
 */

import { CircleAlert, Filter, FlaskConical, Minus, Plus, RefreshCw, Route, Scan, Sparkles } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import * as cam from "./graph/camera";
import { DEFAULT_BUDGET, lodKey, selectVisible, type LodResult } from "./graph/lod";
import { FileViewModal, NodePanel, PathPanel, SearchBox } from "./graph/panels";
import { GraphLayout, nodeRadius, type SimLink, type SimNode } from "./graph/simulation";
import { color, edgeKey, GROUP_COLOR, GROUP_LABEL, TYPE_GROUP } from "./graph/theme";
import type { BridgeEntity, DocumentFile, GraphData, GraphEdge, GraphNode } from "../types";

const EMPTY_LOD: LodResult = { nodes: [], links: [], totalNodes: 0, totalLinks: 0, budgetLimited: false };

/** Re-tile at most this often while the camera is in motion. The overscan in
 * lod.ts covers half a screen in every direction, so a pan can outrun the
 * render set only if it crosses more than that within one interval. */
const RETILE_INTERVAL_MS = 140;
const MINIMAP_INTERVAL_MS = 90;

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
  // Totals survive a merge: expanding a neighbourhood adds to the view but does not make
  // it the whole graph, so the denominator it is a sample OF is still the right one.
  const totals = incoming.totals ?? base?.totals;
  return {
    merged: {
      nodes: [...nodeMap.values()],
      edges: [...edgeMap.values()],
      totals,
      truncated: totals ? [...edgeMap.values()].length < totals.edges : undefined,
    },
    addedIds,
  };
}

type DragState =
  | { mode: "pan"; pointerId: number; lastX: number; lastY: number; lastT: number; moved: number }
  | { mode: "node"; pointerId: number; node: SimNode; moved: number; grabbed: boolean };

/** Pointer travel (px) before a press on a node counts as a drag rather than a
 * click. Below it nothing is pinned and the layout is never reheated: a click
 * must leave the graph exactly where it is, or the node moves out from under
 * the second click of a double-click. */
const NODE_DRAG_SLOP = 4;

const MINIMAP_W = 168;
const MINIMAP_H = 112;
/** The minimap is an orientation aid, not a second graph — past this many dots
 * it shows the best-connected ones and stays legible. */
const MINIMAP_MAX_DOTS = 1200;

export function GraphView() {
  const [data, setData] = useState<GraphData | null>(null);
  /** Bumped whenever the layout's node/link *set* changes. The layout objects
   * themselves are mutated in place by the simulation, so this is what tells
   * memos to look again. */
  const [dataVersion, setDataVersion] = useState(0);
  const [lod, setLod] = useState<LodResult>(EMPTY_LOD);
  const [size, setSize] = useState({ w: 960, h: 640 });
  const [selected, setSelected] = useState<string | null>(null);
  const [fileView, setFileView] = useState<DocumentFile | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [bridges, setBridges] = useState<BridgeEntity[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [hiddenRels, setHiddenRels] = useState<Set<string>>(new Set());
  const [hideNoise, setHideNoise] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [pathMode, setPathMode] = useState(false);
  const [pathEdgeKeys, setPathEdgeKeys] = useState<Set<string>>(new Set());
  const [pathHops, setPathHops] = useState<GraphEdge[] | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [pathBusy, setPathBusy] = useState(false);
  const [newIds, setNewIds] = useState<Set<string>>(new Set());
  const [ripple, setRipple] = useState<{ id: string; key: number } | null>(null);

  const layoutRef = useRef<GraphLayout>(null as unknown as GraphLayout);
  if (layoutRef.current === null) layoutRef.current = new GraphLayout();

  const svgRef = useRef<SVGSVGElement>(null);
  const worldRef = useRef<SVGGElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const minimapRef = useRef<SVGSVGElement>(null);
  const minimapRectRef = useRef<SVGRectElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  const camRef = useRef<cam.Camera>({ x: 480, y: 320, k: 1 });
  const targetRef = useRef<cam.Camera>({ x: 480, y: 320, k: 1 });
  const velRef = useRef<cam.Vec>({ x: 0, y: 0 });
  const sizeRef = useRef(size);
  const rectRef = useRef<DOMRect | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const pressedNodeRef = useRef<SimNode | null>(null);
  const hoverPosRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });
  const hoveredRef = useRef<string | null>(null);

  const rafRef = useRef(0);
  const runningRef = useRef(false);
  const lastTsRef = useRef(0);
  const hotRef = useRef(false);
  const wasHotRef = useRef(false);
  const markScaleRef = useRef(1);
  const lodKeyRef = useRef("");
  const lodEpochRef = useRef(0);
  const lastRetileRef = useRef(0);
  const lastMinimapRef = useRef(0);
  const minimapExtentRef = useRef({ x: -400, y: -300, w: 800, h: 600 });
  const minimapDragging = useRef(false);

  const drawRef = useRef<{
    nodes: { el: SVGGElement; n: SimNode }[];
    edges: { el: SVGLineElement; s: SimNode; t: SimNode }[];
    dots: { el: SVGCircleElement; n: SimNode }[];
  }>({ nodes: [], edges: [], dots: [] });

  const filteredRef = useRef<{ nodes: SimNode[]; links: SimLink[] }>({ nodes: [], links: [] });
  const pinnedRef = useRef<Set<string>>(new Set());

  const bridgeMap = useMemo(() => new Map(bridges.map((b) => [b.id, b])), [bridges]);
  const reduceMotion = useRef(
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches,
  );

  /* ---------------------------------------------------------------- painting
   * All of this writes attributes directly. It is called from the render loop
   * and from the layout effect that (re)binds elements after a React commit —
   * never from render itself. */

  const paintMinimap = useCallback(() => {
    const svg = minimapRef.current;
    if (!svg) return;
    const ext = cam.extentOf(layoutRef.current.nodes);
    const pad = 60;
    const box = ext
      ? {
          x: ext.minX - pad,
          y: ext.minY - pad,
          w: Math.max(200, ext.maxX - ext.minX + pad * 2),
          h: Math.max(200, ext.maxY - ext.minY + pad * 2),
        }
      : { x: -400, y: -300, w: 800, h: 600 };
    minimapExtentRef.current = box;
    svg.setAttribute("viewBox", `${box.x} ${box.y} ${box.w} ${box.h}`);
    const span = Math.max(box.w, box.h);
    const dotR = (span / 150).toFixed(2);
    for (const d of drawRef.current.dots) {
      d.el.setAttribute("cx", d.n.x.toFixed(1));
      d.el.setAttribute("cy", d.n.y.toFixed(1));
      d.el.setAttribute("r", dotR);
    }
    const rect = minimapRectRef.current;
    if (rect) {
      const v = cam.viewportRect(camRef.current, sizeRef.current.w, sizeRef.current.h);
      rect.setAttribute("x", v.x.toFixed(1));
      rect.setAttribute("y", v.y.toFixed(1));
      rect.setAttribute("width", Math.max(1, v.w).toFixed(1));
      rect.setAttribute("height", Math.max(1, v.h).toFixed(1));
      rect.setAttribute("stroke-width", (span / 220).toFixed(2));
    }
  }, []);

  const paint = useCallback(
    (force = false) => {
      const c = camRef.current;
      const world = worldRef.current;
      if (world) {
        world.setAttribute("transform", `translate(${c.x.toFixed(2)} ${c.y.toFixed(2)}) scale(${c.k.toFixed(5)})`);
        // Labels are a zoom band, toggled with one class so the fade costs no
        // React render: below this scale they would overlap into noise.
        world.classList.toggle("graph-labels-on", c.k >= cam.LABEL_MIN_SCALE);
      }
      const ms = cam.markScale(c.k);
      const scaleChanged = Math.abs(ms / markScaleRef.current - 1) > 0.0015;
      const moving = hotRef.current;
      if (force || moving || scaleChanged) {
        markScaleRef.current = ms;
        const s = ms.toFixed(4);
        for (const it of drawRef.current.nodes) {
          it.el.setAttribute("transform", `translate(${it.n.x.toFixed(1)} ${it.n.y.toFixed(1)}) scale(${s})`);
        }
      }
      if (force || moving) {
        for (const it of drawRef.current.edges) {
          it.el.setAttribute("x1", it.s.x.toFixed(1));
          it.el.setAttribute("y1", it.s.y.toFixed(1));
          it.el.setAttribute("x2", it.t.x.toFixed(1));
          it.el.setAttribute("y2", it.t.y.toFixed(1));
        }
      }
    },
    [],
  );

  /** Recompute the drawn subset for the current camera. Cheap when nothing
   * moved: the quantised viewport key short-circuits it. */
  const refreshLod = useCallback(
    (force = false) => {
      const view = cam.viewportRect(camRef.current, sizeRef.current.w, sizeRef.current.h);
      const key = `${lodKey(view)}|${lodEpochRef.current}`;
      if (!force && key === lodKeyRef.current) return;
      lodKeyRef.current = key;
      const f = filteredRef.current;
      setLod(selectVisible(f.nodes, f.links, view, pinnedRef.current, DEFAULT_BUDGET));
    },
    [],
  );

  /* ------------------------------------------------------------ render loop
   * One rAF loop drives camera easing, fling inertia and the force simulation,
   * so they share a clock. It stops itself as soon as everything is at rest —
   * an idle graph costs nothing — and any interaction calls wake(). */

  const stepRef = useRef<(ts: number) => void>(() => {});
  stepRef.current = (ts: number) => {
    const dt = Math.min(0.05, Math.max(0.001, (ts - lastTsRef.current) / 1000));
    lastTsRef.current = ts;
    let busy = false;

    const dragging = dragRef.current !== null;
    if (dragging) {
      busy = true;
    } else if (cam.speed(velRef.current) > cam.MIN_FLING_SPEED) {
      const v = velRef.current;
      targetRef.current = cam.panBy(targetRef.current, v.x * dt, v.y * dt);
      velRef.current = cam.decayVelocity(v, dt);
      busy = true;
    } else if (velRef.current.x || velRef.current.y) {
      velRef.current = { x: 0, y: 0 };
    }

    if (!cam.cameraSettled(camRef.current, targetRef.current)) {
      camRef.current = reduceMotion.current
        ? { ...targetRef.current }
        : cam.approach(camRef.current, targetRef.current, dt, dragging ? 26 : cam.CAMERA_STIFFNESS);
      busy = true;
    } else if (camRef.current !== targetRef.current) {
      camRef.current = { ...targetRef.current };
    }

    const hot = layoutRef.current.hot();
    hotRef.current = hot;
    if (hot) {
      layoutRef.current.tick();
      busy = true;
    }

    paint();

    if (ts - lastMinimapRef.current > MINIMAP_INTERVAL_MS) {
      lastMinimapRef.current = ts;
      paintMinimap();
    }
    if (wasHotRef.current && !hot) {
      // The layout just came to rest: positions are final, so re-tile against
      // where things actually ended up.
      lodEpochRef.current += 1;
      refreshLod(true);
    } else if (ts - lastRetileRef.current > RETILE_INTERVAL_MS) {
      lastRetileRef.current = ts;
      refreshLod();
    }
    wasHotRef.current = hot;

    if (busy) {
      rafRef.current = requestAnimationFrame((t) => stepRef.current(t));
    } else {
      runningRef.current = false;
      refreshLod();
    }
  };

  const wake = useCallback(() => {
    if (runningRef.current) return;
    runningRef.current = true;
    lastTsRef.current = performance.now();
    rafRef.current = requestAnimationFrame((t) => stepRef.current(t));
  }, []);

  useEffect(() => () => cancelAnimationFrame(rafRef.current), []);

  /* ------------------------------------------------------------------- data */

  const applyData = useCallback(
    (
      next: GraphData,
      opts: { focusIds?: string[]; warm?: number; alpha?: number; fit?: boolean } = {},
    ) => {
      const gl = layoutRef.current;
      const { added } = gl.setData(next);
      if (opts.warm) gl.warmup(opts.warm);
      gl.reheat(opts.alpha ?? 0.5);
      setData(next);
      setDataVersion((v) => v + 1);
      if (added.size) {
        setNewIds(added);
        window.setTimeout(() => setNewIds(new Set()), 480);
      }
      if (opts.fit !== false) {
        const focus = opts.focusIds?.length ? new Set(opts.focusIds) : null;
        const pts = focus ? gl.nodes.filter((n) => focus.has(n.id)) : gl.nodes;
        const fit = cam.fitCamera(pts.length ? pts : gl.nodes, sizeRef.current.w, sizeRef.current.h);
        if (fit) {
          targetRef.current = fit;
          velRef.current = { x: 0, y: 0 };
        }
      }
      lodEpochRef.current += 1;
      wake();
    },
    [wake],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.graph(undefined, 400);
      // Warm the first snapshot most of the way so the view opens on a
      // readable layout, then let the live loop finish the settle visibly.
      applyData(d, { warm: 240, alpha: 0.22 });
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [applyData]);

  const expand = useCallback(
    async (entity: string, opts: { focus?: boolean } = {}) => {
      try {
        const d = await api.graph(entity, 200);
        const { merged } = mergeGraphData(data, d);
        applyData(merged, { focusIds: d.nodes.map((n) => n.id), warm: 30, alpha: 0.55 });
        if (opts.focus !== false) setSelected(d.entity?.id ?? entity);
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
      }
    },
    [data, applyData],
  );

  useEffect(() => {
    load();
    api.graphBridges(16).then(setBridges).catch(() => setBridges([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --------------------------------------------------------------- derived */

  const names = useMemo(() => new Map((data?.nodes ?? []).map((n) => [n.id, n.name])), [data]);
  const selectedNode = data?.nodes.find((n) => n.id === selected) ?? null;
  const relTypes = useMemo(() => [...new Set((data?.edges ?? []).map((e) => e.rel))].sort(), [data]);
  const nodeTypes = useMemo(() => [...new Set((data?.nodes ?? []).map((n) => n.type))].sort(), [data]);
  const groupsPresent = useMemo(
    () => [...new Set((data?.nodes ?? []).map((n) => TYPE_GROUP[n.type] ?? "other"))],
    [data],
  );

  /** Test scaffolding and vendored libraries, by the deterministic layer tag. Measured on
   * a live corpus these are the two biggest categories of code entity — 33.7% and 16.0%
   * of defining files — so they can crowd real architecture out of a sampled view. Hiding
   * is opt-in and states its own count in the toolbar rather than quietly shrinking the
   * graph; nothing is hidden by default. */
  const noiseCount = useMemo(
    () => (data?.nodes ?? []).filter((n) => n.layer === "test" || n.layer === "vendor").length,
    [data],
  );

  const filtered = useMemo(() => {
    const gl = layoutRef.current;
    let nodes = hiddenTypes.size ? gl.nodes.filter((n) => !hiddenTypes.has(n.type)) : gl.nodes;
    if (hideNoise) nodes = nodes.filter((n) => n.layer !== "test" && n.layer !== "vendor");
    const narrowed = hiddenTypes.size > 0 || hideNoise;
    const ok = narrowed ? new Set(nodes.map((n) => n.id)) : null;
    const links = gl.links.filter(
      (l) =>
        !hiddenRels.has(l.edge.rel) && (!ok || (ok.has(l.source.id) && ok.has(l.target.id))),
    );
    return { nodes, links };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataVersion, hiddenTypes, hiddenRels, hideNoise]);

  useEffect(() => {
    filteredRef.current = filtered;
    refreshLod(true);
  }, [filtered, refreshLod]);

  const neighborIds = useMemo(() => {
    if (!selected || !data) return new Set<string>();
    const ids = new Set<string>([selected]);
    for (const e of data.edges) {
      if (e.src === selected) ids.add(e.dst);
      if (e.dst === selected) ids.add(e.src);
    }
    return ids;
  }, [selected, data]);

  // Anything the user is actively working with survives culling — losing the
  // selected node or a path hop to a viewport cull would break the very
  // interaction that put it there.
  useEffect(() => {
    const pinned = new Set<string>(neighborIds);
    for (const h of pathHops ?? []) {
      pinned.add(h.src);
      pinned.add(h.dst);
    }
    pinnedRef.current = pinned;
    refreshLod(true);
  }, [neighborIds, pathHops, refreshLod]);

  const minimapNodes = useMemo(() => {
    const ns = layoutRef.current.nodes;
    if (ns.length <= MINIMAP_MAX_DOTS) return ns;
    return ns.slice().sort((a, b) => b.degree - a.degree).slice(0, MINIMAP_MAX_DOTS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataVersion]);

  /* ------------------------------------------------- element (re)binding */

  useLayoutEffect(() => {
    const root = worldRef.current;
    const byId = layoutRef.current.byId;
    const nodes: { el: SVGGElement; n: SimNode }[] = [];
    const edges: { el: SVGLineElement; s: SimNode; t: SimNode }[] = [];
    if (root) {
      root.querySelectorAll<SVGGElement>("g[data-nid]").forEach((el) => {
        const n = byId.get(el.dataset.nid!);
        if (n) nodes.push({ el, n });
      });
      root.querySelectorAll<SVGLineElement>("line[data-src]").forEach((el) => {
        const s = byId.get(el.dataset.src!);
        const t = byId.get(el.dataset.dst!);
        if (s && t) edges.push({ el, s, t });
      });
    }
    const dots: { el: SVGCircleElement; n: SimNode }[] = [];
    minimapRef.current?.querySelectorAll<SVGCircleElement>("circle[data-mid]").forEach((el) => {
      const n = byId.get(el.dataset.mid!);
      if (n) dots.push({ el, n });
    });
    drawRef.current = { nodes, edges, dots };
    // Newly-mounted elements carry no coordinates yet — place them before the
    // browser paints, or they flash at the origin.
    paint(true);
    paintMinimap();
  }, [lod, minimapNodes, paint, paintMinimap]);

  /* ------------------------------------------------------------- viewport */

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      rectRef.current = r;
      const next = { w: Math.max(1, r.width), h: Math.max(1, r.height) };
      const prev = sizeRef.current;
      sizeRef.current = next;
      setSize(next);
      // Keep the world point at the canvas centre put while the panel resizes.
      const dx = (next.w - prev.w) / 2;
      const dy = (next.h - prev.h) / 2;
      targetRef.current = cam.panBy(targetRef.current, dx, dy);
      camRef.current = cam.panBy(camRef.current, dx, dy);
      lodEpochRef.current += 1;
      wake();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [wake]);

  /* ---------------------------------------------------------- interaction */

  /** Zoom-out floor tied to the content: you can pull back to ~2.5x the
   * everything-fits view and no further, so the graph can never be lost as a
   * speck in an empty field. */
  const minZoom = useCallback(() => {
    const fit = cam.fitCamera(layoutRef.current.nodes, sizeRef.current.w, sizeRef.current.h);
    return fit ? Math.max(cam.MIN_SCALE, fit.k * 0.4) : cam.MIN_SCALE;
  }, []);

  const zoomAt = useCallback(
    (factor: number, sx: number, sy: number) => {
      targetRef.current = cam.zoomAround(targetRef.current, factor, sx, sy, minZoom());
      velRef.current = { x: 0, y: 0 };
      // The graph slides under a stationary pointer while zooming, so any
      // tooltip is about to be about the wrong node; the next move re-hovers.
      if (hoveredRef.current) {
        hoveredRef.current = null;
        setHovered(null);
      }
      wake();
    },
    [wake, minZoom],
  );

  const zoomCentre = useCallback(
    (factor: number) => zoomAt(factor, sizeRef.current.w / 2, sizeRef.current.h / 2),
    [zoomAt],
  );

  const fitAll = useCallback(() => {
    const fit = cam.fitCamera(layoutRef.current.nodes, sizeRef.current.w, sizeRef.current.h);
    if (!fit) return;
    targetRef.current = fit;
    velRef.current = { x: 0, y: 0 };
    wake();
  }, [wake]);

  // Trackpad pinch arrives as a wheel event with ctrlKey=true (there is no
  // cross-browser gesture event for it). React's onWheel is registered
  // passively, so preventDefault there is ignored and the browser zooms the
  // whole page underneath the canvas — a native non-passive listener is the
  // only way to absorb it.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = rectRef.current ?? el.getBoundingClientRect();
      // Continuous, so a precision trackpad feels analogue and a notched wheel
      // still moves a satisfying amount per detent.
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const delta = Math.max(-400, Math.min(400, e.deltaY * unit));
      zoomAt(Math.exp(-delta * 0.0022), e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const positionTooltip = useCallback(() => {
    const t = tooltipRef.current;
    if (!t) return;
    const { x, y } = hoverPosRef.current;
    const w = sizeRef.current.w;
    // Flip before the pointer once the card would run off the right edge.
    const left = x + 240 > w ? x - 232 : x + 14;
    t.style.transform = `translate(${left}px, ${y + 14}px)`;
  }, []);

  const nodeIdAt = (target: EventTarget | null): string | null => {
    const el = (target as Element | null)?.closest?.("g[data-nid]");
    return el?.getAttribute("data-nid") ?? null;
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    rectRef.current = e.currentTarget.getBoundingClientRect();
    svgRef.current?.setPointerCapture?.(e.pointerId);
    velRef.current = { x: 0, y: 0 };
    const id = nodeIdAt(e.target);
    const node = id ? layoutRef.current.byId.get(id) : undefined;
    pressedNodeRef.current = node ?? null;
    if (node) {
      // Deliberately NOT pinned or reheated yet — see NODE_DRAG_SLOP.
      dragRef.current = { mode: "node", pointerId: e.pointerId, node, moved: 0, grabbed: false };
    } else {
      dragRef.current = { mode: "pan", pointerId: e.pointerId, lastX: e.clientX, lastY: e.clientY, lastT: performance.now(), moved: 0 };
      // Snapping the target onto the live camera stops a half-finished glide
      // from fighting the new drag.
      targetRef.current = { ...camRef.current };
    }
    wake();
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const rect = rectRef.current ?? e.currentTarget.getBoundingClientRect();
    hoverPosRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    positionTooltip();

    const drag = dragRef.current;
    if (!drag) {
      const id = nodeIdAt(e.target);
      if (id !== hoveredRef.current) {
        hoveredRef.current = id;
        setHovered(id);
      }
      return;
    }
    if (drag.pointerId !== e.pointerId) return;

    if (drag.mode === "node") {
      const w = cam.toWorld(camRef.current, hoverPosRef.current.x, hoverPosRef.current.y);
      drag.moved += Math.hypot(w.x - drag.node.x, w.y - drag.node.y) * camRef.current.k;
      if (!drag.grabbed && drag.moved < NODE_DRAG_SLOP) return;
      drag.grabbed = true;
      drag.node.fx = w.x;
      drag.node.fy = w.y;
      layoutRef.current.reheat(0.34);
      wake();
      return;
    }

    const now = performance.now();
    const dx = e.clientX - drag.lastX;
    const dy = e.clientY - drag.lastY;
    const dt = Math.max(0.008, (now - drag.lastT) / 1000);
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
    drag.lastT = now;
    drag.moved += Math.hypot(dx, dy);
    targetRef.current = cam.panBy(targetRef.current, dx, dy);
    velRef.current = cam.blendVelocity(velRef.current, { x: dx / dt, y: dy / dt });
    wake();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    svgRef.current?.releasePointerCapture?.(e.pointerId);
    if (!drag) return;

    if (drag.mode === "node") {
      if (drag.grabbed) {
        // Let go and the node rejoins the simulation rather than staying
        // pinned where it was dropped — the graph is a live system, not a
        // diagram.
        drag.node.fx = null;
        drag.node.fy = null;
        layoutRef.current.reheat(0.16);
      } else if (e.detail < 2) {
        // e.detail > 1 is the second click of a double-click, which means
        // "expand" — toggling selection off underneath it would be noise.
        setSelected((s) => (s === drag.node.id ? null : drag.node.id));
      }
      wake();
      return;
    }

    if (drag.moved < NODE_DRAG_SLOP) {
      setSelected(null);
      velRef.current = { x: 0, y: 0 };
    } else if (!reduceMotion.current && performance.now() - drag.lastT < 90) {
      // Only a pointer still moving at release throws the canvas; a pause
      // before letting go means "put it down here".
      velRef.current = cam.clampFling(velRef.current);
    } else {
      velRef.current = { x: 0, y: 0 };
    }
    wake();
  };

  // Uses the node the pointer was pressed on, NOT e.target: a dblclick is
  // dispatched to the nearest common ancestor of its two clicks, so the moment
  // anything shifts between them the target degrades to the <svg> and the
  // gesture is silently lost.
  const onDoubleClick = (e: React.MouseEvent) => {
    const node = pressedNodeRef.current;
    if (!node) return;
    e.stopPropagation();
    setRipple({ id: node.id, key: Date.now() });
    expand(node.id, { focus: false });
  };

  /* ----------------------------------------------------------- path finder */

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
        for (const h of res.path) {
          pathNodeIds.add(h.src);
          pathNodeIds.add(h.dst);
        }
        const known = new Set((data?.nodes ?? []).map((n) => n.id));
        const pathNodes: GraphNode[] = [res.a, res.b];
        for (const h of res.path) {
          if (!known.has(h.src)) pathNodes.push({ id: h.src, name: names.get(h.src) ?? h.src, type: "service" });
          if (!known.has(h.dst)) pathNodes.push({ id: h.dst, name: names.get(h.dst) ?? h.dst, type: "service" });
        }
        const { merged } = mergeGraphData(data, { nodes: pathNodes, edges: res.path });
        setPathEdgeKeys(new Set(res.path.map(edgeKey)));
        setPathHops(res.path);
        applyData(merged, { focusIds: [...pathNodeIds], warm: 40, alpha: 0.5 });
      } catch (e) {
        setPathError(String(e instanceof Error ? e.message : e));
      } finally {
        setPathBusy(false);
      }
    },
    [data, names, applyData],
  );

  /* -------------------------------------------------------------- minimap */

  const panMinimapTo = useCallback(
    (clientX: number, clientY: number) => {
      const rect = minimapRef.current?.getBoundingClientRect();
      if (!rect) return;
      const ext = minimapExtentRef.current;
      const worldX = ext.x + ((clientX - rect.left) / rect.width) * ext.w;
      const worldY = ext.y + ((clientY - rect.top) / rect.height) * ext.h;
      const t = targetRef.current;
      targetRef.current = {
        k: t.k,
        x: sizeRef.current.w / 2 - worldX * t.k,
        y: sizeRef.current.h / 2 - worldY * t.k,
      };
      velRef.current = { x: 0, y: 0 };
      wake();
    },
    [wake],
  );

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

  const hoveredNode = hovered ? layoutRef.current.byId.get(hovered) : undefined;
  const drawnHint =
    lod.nodes.length < filtered.nodes.length ? ` · ${lod.nodes.length} in view` : "";
  // The server samples a connected core out of a graph far too large to draw. Saying so —
  // with the real denominator — is the difference between "this is the organization" and
  // "this is a readable slice of it".
  const sampledHint =
    data?.truncated && data.totals
      ? ` · sample of ${data.totals.edges.toLocaleString()}`
      : "";

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
          {noiseCount > 0 && (
            <button
              onClick={() => setHideNoise((v) => !v)}
              title="Test projects and vendored libraries, tagged deterministically from each defining file's path"
              className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] ${
                hideNoise ? "bg-accent-soft text-accent" : "bg-fill text-muted hover:text-ink"
              }`}
            >
              <FlaskConical size={12} /> {hideNoise ? `${noiseCount} hidden` : "Tests & vendor"}
            </button>
          )}
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
            {data
              ? `${filtered.nodes.length}/${data.nodes.length} entities · ${filtered.links.length}/${data.edges.length} relationships${drawnHint}${sampledHint}`
              : ""}
          </span>
        </div>

        {showFilters && (
          <div className="mx-5 mb-2 flex flex-wrap items-center gap-3 rounded-lg bg-panel px-4 py-2.5 backdrop-blur-xl md:mx-8">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 font-mono text-[9.5px] uppercase text-faint">Types</span>
              {nodeTypes.map((t) => (
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
        <div ref={wrapRef} className="relative mx-5 mb-5 min-h-0 flex-1 overflow-hidden rounded-lg bg-fill md:mx-8">
          {error && (
            <div className="absolute inset-x-0 top-3 z-10 mx-auto w-fit rounded-full bg-danger-soft px-4 py-1.5 font-mono text-[11px] text-danger">
              <CircleAlert size={12} className="mr-1.5 inline" />
              {error}
            </div>
          )}
          {!loading && data && data.nodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center px-8 text-center text-[13.5px] leading-relaxed text-muted">
              Nothing in the knowledge graph yet. Sync a repo with dependency manifests,
              connect Jira/Octopus, or teach facts that mention ticket keys — relationships
              show up here as they are learned.
            </div>
          )}
          <svg
            ref={svgRef}
            className="h-full w-full cursor-grab touch-none select-none active:cursor-grabbing"
            viewBox={`0 0 ${size.w} ${size.h}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerLeave={() => { hoveredRef.current = null; setHovered(null); }}
            onDoubleClick={onDoubleClick}
          >
            <g ref={worldRef}>
              <g>
                {lod.links.map((l) => {
                  const e = l.edge;
                  const active = !selected || e.src === selected || e.dst === selected;
                  const onPath = pathEdgeKeys.has(edgeKey(e));
                  return (
                    <line
                      key={edgeKey(e)}
                      data-src={e.src}
                      data-dst={e.dst}
                      vectorEffect="non-scaling-stroke"
                      stroke={onPath ? "var(--accent)" : "var(--muted)"}
                      strokeOpacity={onPath ? 0.9 : active ? 0.45 : 0.12}
                      strokeWidth={onPath ? 2.4 : active && selected ? 1.6 : 1}
                      className={onPath ? "graph-edge-flow" : undefined}
                      style={{ transition: "stroke-opacity 200ms ease, stroke-width 200ms ease" }}
                    />
                  );
                })}
              </g>
              <g>
                {lod.nodes.map((n) => {
                  const dim = selected != null && !neighborIds.has(n.id) && !pathEdgeKeys.size;
                  const r = nodeRadius(n.degree);
                  const keepLabel = n.id === hovered || neighborIds.has(n.id) || pathEdgeKeys.size > 0;
                  const bridge = bridgeMap.get(n.id);
                  return (
                    <g
                      key={n.id}
                      data-nid={n.id}
                      opacity={dim ? 0.25 : 1}
                      style={{ transition: "opacity 200ms ease" }}
                    >
                      <g className={newIds.has(n.id) ? "graph-node-enter" : undefined}>
                        <g className="graph-node-inner cursor-pointer">
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
                      <text
                        y={r + 11}
                        textAnchor="middle"
                        className={keepLabel ? "graph-label graph-label-keep" : "graph-label"}
                        style={{ fill: "var(--muted)", fontSize: 9.5, fontFamily: "Geist Mono, monospace" }}
                      >
                        {n.name.length > 30 ? n.name.slice(0, 29) + "…" : n.name}
                      </text>
                    </g>
                  );
                })}
              </g>
            </g>
          </svg>
          {/* hover tooltip — screen-space, positioned imperatively so following
              the pointer never re-renders the canvas */}
          {hoveredNode && (
            <div
              ref={tooltipRef}
              className="pointer-events-none absolute left-0 top-0 z-20 rounded-md bg-raised2 px-2.5 py-1.5 text-[11.5px] shadow-lg"
              style={{ maxWidth: 220 }}
            >
              <div className="truncate font-semibold text-ink">{hoveredNode.name}</div>
              <div className="mt-0.5 flex items-center gap-1.5">
                <span
                  className="inline-block rounded-full px-2 py-px font-mono text-[9px] uppercase tracking-[0.14em]"
                  style={{ color: color(hoveredNode.type), background: "var(--fill)" }}
                >
                  {hoveredNode.type}
                </span>
                <span className="font-mono text-[9.5px] text-faint">{hoveredNode.degree} links</span>
              </div>
              {(() => {
                const b = bridgeMap.get(hoveredNode.id);
                return b && b.source_count > 1 ? (
                  <div className="mt-0.5 flex items-center gap-1 font-mono text-[9.5px] text-accent">
                    <Sparkles size={9} /> spans {b.source_count} sources
                  </div>
                ) : null;
              })()}
            </div>
          )}
          {/* legend */}
          {groupsPresent.length > 0 && (
            <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-full bg-panel px-3.5 py-1.5 backdrop-blur-xl">
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
          {/* overview minimap + zoom controls — slide clear of the inspector
              rather than hiding behind it */}
          {minimapNodes.length > 0 && (
            <div
              className={
                "absolute bottom-3 flex flex-col items-end gap-2 transition-[right] duration-200 " +
                (selectedNode ? "right-[310px]" : "right-3")
              }
            >
              <div className="overflow-hidden rounded-md bg-panel p-1 backdrop-blur-xl">
                <svg
                  ref={minimapRef}
                  width={MINIMAP_W}
                  height={MINIMAP_H}
                  className="cursor-pointer touch-none rounded-[3px]"
                  style={{ background: "var(--fill)" }}
                  onPointerDown={(e) => {
                    minimapDragging.current = true;
                    (e.target as Element).setPointerCapture?.(e.pointerId);
                    panMinimapTo(e.clientX, e.clientY);
                  }}
                  onPointerMove={(e) => { if (minimapDragging.current) panMinimapTo(e.clientX, e.clientY); }}
                  onPointerUp={() => { minimapDragging.current = false; }}
                  onPointerLeave={() => { minimapDragging.current = false; }}
                >
                  {minimapNodes.map((n) => (
                    <circle key={n.id} data-mid={n.id} fill={color(n.type)} fillOpacity={0.75} />
                  ))}
                  <rect
                    ref={minimapRectRef}
                    fill="var(--accent)"
                    fillOpacity={0.08}
                    stroke="var(--accent)"
                  />
                </svg>
              </div>
              <div className="flex flex-col overflow-hidden rounded-full bg-panel backdrop-blur-xl">
                <button
                  onClick={() => zoomCentre(1.35)}
                  title="Zoom in"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Plus size={14} />
                </button>
                <div className="h-px bg-fill2" />
                <button
                  onClick={() => zoomCentre(1 / 1.35)}
                  title="Zoom out"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Minus size={14} />
                </button>
                <div className="h-px bg-fill2" />
                <button
                  onClick={fitAll}
                  title="Fit everything in view"
                  className="flex h-8 w-8 items-center justify-center text-muted hover:text-ink"
                >
                  <Scan size={13} />
                </button>
              </div>
            </div>
          )}
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
        </div>
      </div>
      <FileViewModal file={fileView} onClose={() => setFileView(null)} />
    </div>
  );
}
