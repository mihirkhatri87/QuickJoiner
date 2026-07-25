/* Camera model for the knowledge-graph canvas.
 *
 * The canvas used to be driven by an SVG `viewBox` that was snapped straight
 * into React state on every wheel tick / pointer move: each interaction was a
 * discrete jump, and because node radii were expressed in *world* units, an
 * auto-fit over a wide layout (e.g. after "expand neighbors" merged a few
 * hundred entities) rendered every node as a one-pixel speck. Both problems
 * are camera problems, so they're solved here:
 *
 *   - the camera is a continuous {x, y, k} transform (screen = world*k + xy)
 *     that a render loop *eases* toward a target, with pointer-throw inertia,
 *     so panning glides and zooming settles instead of stepping;
 *   - `markScale` converts the camera scale into the counter-scale a node's
 *     mark must carry so its painted size stays essentially constant on
 *     screen — the fix for "zoom out and everything becomes tiny".
 *
 * Everything here is pure: no React, no DOM. The render loop in GraphView
 * owns the mutable camera and calls these to advance it.
 */

export interface Camera {
  /** Screen-space translation, in CSS pixels. */
  x: number;
  y: number;
  /** Scale: screen pixels per world unit. */
  k: number;
}

export interface Vec {
  x: number;
  y: number;
}

/** Zoom-out is bounded well below "everything is a speck", zoom-in well above
 * "one node fills the canvas" — a free-running viewBox had neither bound. */
export const MIN_SCALE = 0.05;
export const MAX_SCALE = 6;

/** Exponential approach rate (per second) of the live camera to its target.
 * High enough that a drag feels directly connected, low enough that a fit or
 * a wheel tick reads as a glide rather than a cut. */
export const CAMERA_STIFFNESS = 16;

/** Pointer-throw inertia: velocity decays exponentially at this rate per
 * second and is dropped entirely below MIN_FLING_SPEED. Capped so a violent
 * flick can't launch the graph out of the world. */
export const FLING_FRICTION = 3.4;
export const MIN_FLING_SPEED = 16;
export const MAX_FLING_SPEED = 3600;

/** Below this scale, per-node labels would overlap into noise, so the canvas
 * shows marks only (hovered / selected / path nodes keep their label). */
export const LABEL_MIN_SCALE = 0.7;

/** `minK` lets the caller raise the zoom-out floor relative to the content: a
 * fixed floor lets you zoom until the whole graph is a speck in an empty
 * field, which is never a view anyone wanted. */
export function clampScale(k: number, minK = MIN_SCALE): number {
  return Math.min(MAX_SCALE, Math.max(Math.max(MIN_SCALE, minK), k));
}

export function toWorld(cam: Camera, sx: number, sy: number): Vec {
  return { x: (sx - cam.x) / cam.k, y: (sy - cam.y) / cam.k };
}

export function toScreen(cam: Camera, wx: number, wy: number): Vec {
  return { x: wx * cam.k + cam.x, y: wy * cam.k + cam.y };
}

/** Zoom by `factor` while keeping the world point currently under (sx, sy)
 * pinned there — the difference between "zoom toward my cursor" and "zoom to
 * the middle and hunt for what I was looking at". */
export function zoomAround(cam: Camera, factor: number, sx: number, sy: number, minK = MIN_SCALE): Camera {
  const k = clampScale(cam.k * factor, minK);
  const f = k / cam.k;
  return { k, x: sx - (sx - cam.x) * f, y: sy - (sy - cam.y) * f };
}

export function panBy(cam: Camera, dx: number, dy: number): Camera {
  return { ...cam, x: cam.x + dx, y: cam.y + dy };
}

/** World rectangle currently visible — what the minimap draws as its viewport
 * indicator, and the inverse of `fitCamera`. */
export function viewportRect(cam: Camera, viewW: number, viewH: number) {
  return { x: -cam.x / cam.k, y: -cam.y / cam.k, w: viewW / cam.k, h: viewH / cam.k };
}

export interface Extent {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export function extentOf(points: readonly Vec[]): Extent | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return minX === Infinity ? null : { minX, minY, maxX, maxY };
}

/** Camera that frames `points` inside a viewW × viewH canvas.
 *
 * `maxK` matters: fitting one or two nodes would otherwise zoom to the scale
 * cap and lose all sense of place, so a fit never zooms *in* past a readable
 * scale — it only ever pulls back far enough to show everything asked for. */
export function fitCamera(
  points: readonly Vec[],
  viewW: number,
  viewH: number,
  pad = 90,
  maxK = 1.25,
): Camera | null {
  const e = extentOf(points);
  if (!e || viewW <= 0 || viewH <= 0) return null;
  const w = Math.max(1, e.maxX - e.minX);
  const h = Math.max(1, e.maxY - e.minY);
  const usableW = Math.max(40, viewW - pad * 2);
  const usableH = Math.max(40, viewH - pad * 2);
  const k = clampScale(Math.min(maxK, Math.min(usableW / w, usableH / h)));
  const cx = (e.minX + e.maxX) / 2;
  const cy = (e.minY + e.maxY) / 2;
  return { k, x: viewW / 2 - cx * k, y: viewH / 2 - cy * k };
}

/** Frame-rate-independent exponential approach: the same visual glide whether
 * the loop runs at 60, 120 or a stuttering 24 fps. Scale is interpolated
 * geometrically (constant *ratio* per unit time) because that is how zoom is
 * perceived — a linear scale lerp lurches at one end. */
export function approach(cur: Camera, target: Camera, dt: number, stiffness = CAMERA_STIFFNESS): Camera {
  const t = 1 - Math.exp(-stiffness * dt);
  return {
    x: cur.x + (target.x - cur.x) * t,
    y: cur.y + (target.y - cur.y) * t,
    k: cur.k * Math.pow(target.k / cur.k, t),
  };
}

export function cameraSettled(a: Camera, b: Camera): boolean {
  return Math.abs(a.x - b.x) < 0.08 && Math.abs(a.y - b.y) < 0.08 && Math.abs(a.k / b.k - 1) < 0.0008;
}

export function speed(v: Vec): number {
  return Math.hypot(v.x, v.y);
}

export function clampFling(v: Vec): Vec {
  const s = speed(v);
  if (s <= MAX_FLING_SPEED || s === 0) return v;
  const f = MAX_FLING_SPEED / s;
  return { x: v.x * f, y: v.y * f };
}

export function decayVelocity(v: Vec, dt: number, friction = FLING_FRICTION): Vec {
  const f = Math.exp(-friction * dt);
  return { x: v.x * f, y: v.y * f };
}

/** Exponentially-smoothed pointer velocity, so a fling reflects the last few
 * milliseconds of movement rather than one jittery final sample. */
export function blendVelocity(prev: Vec, sample: Vec, weight = 0.28): Vec {
  return { x: prev.x * (1 - weight) + sample.x * weight, y: prev.y * (1 - weight) + sample.y * weight };
}

/** Counter-scale a node mark must carry, in world units, so its painted size
 * stays near-constant on screen across the whole zoom range.
 *
 * Not a flat 1/k: painted size follows k^EXP (clamped), so zooming in still
 * grows marks a little — the graph reads as depth rather than as a fixed
 * overlay — while zooming out can never shrink them to invisible dust, which
 * is exactly what the plain world-unit radii did. */
const MARK_EXP = 0.35;
const MARK_MIN = 0.62;
const MARK_MAX = 1.85;

export function markScale(k: number): number {
  const painted = Math.min(MARK_MAX, Math.max(MARK_MIN, Math.pow(k, MARK_EXP)));
  return painted / k;
}
