/* Shared visual vocabulary for the knowledge-graph view — imported by both the
 * canvas (GraphView) and its side panels, so a colour never means one thing in
 * the graph and another in the evidence list beside it. */

import type { GraphEdge } from "../../types";

// Grouped categorical scheme: three validated hue families (see index.css) plus
// a neutral bucket. 11 distinct entity types can't each carry a safe, distinct
// hue (the design system has 3 non-status categorical hues), so types fold into
// the axis a user actually cares about — text (name + type badge, already shown
// on hover/click) carries the fine-grained identity; color carries the family.
export const TYPE_GROUP: Record<string, string> = {
  repo: "code", project: "code", symbol: "code", module: "code", branch: "code",
  package: "dep",
  service: "org", environment: "org", ticket: "org", team: "org", person: "org",
  merge_request: "org", topic: "org", datastore: "dep", pipeline: "code",
};
export const GROUP_COLOR: Record<string, string> = {
  code: "var(--graph-code)",
  dep: "var(--graph-dep)",
  org: "var(--graph-org)",
};
export const GROUP_LABEL: Record<string, string> = {
  code: "Code",
  dep: "Dependencies",
  org: "Org & ops",
};

export const color = (type: string): string =>
  GROUP_COLOR[TYPE_GROUP[type] ?? ""] ?? "var(--faint)";

export function edgeKey(e: GraphEdge): string {
  return `${e.src}|${e.rel}|${e.dst}|${e.evidence.doc_id}`;
}
