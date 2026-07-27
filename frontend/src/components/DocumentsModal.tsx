/* What a connector has actually ingested — and where you label it.
 *
 * Two jobs in one place, because they're the same question ("what's in here, and how do I
 * refer to it later?"):
 *   1. **See it.** Every document with its chunk count and when it was learned. A source
 *      that reports 200 documents but nothing you recognise is a sync problem you can only
 *      spot by looking.
 *   2. **Label it.** Tags scope questions; `aka` gives something an alternate name. Both
 *      can be attached at three granularities, which is the whole reason this table shows
 *      folders as well as files: labelling a FOLDER is a live rule, so documents ingested
 *      into it later inherit the label with no re-tagging.
 *
 * Labels arrive already resolved per document (the server evaluates the prefix rules), so a
 * row shows inherited labels alongside its own — with the inherited ones marked, since
 * removing one affects every sibling.
 */

import { ChevronDown, ChevronRight, FileArchive, FileText, Folder, Plus, Tag, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { DocLabel, IngestedDoc, SourceDocuments } from "../types";
import { cn } from "./ui";

/** A .zip ingests as ONE document (its members' text concatenated), so this is the only
 * signal the browser has that a row is worth expanding — matches ARCHIVE_EXTENSIONS in
 * ingest/extract.py (just .zip today). */
function isArchive(doc: IngestedDoc): boolean {
  return /\.zip$/i.test(doc.uri) || /\.zip$/i.test(doc.title);
}

/** Everything up to the last separator — the "folder" a document lives in. Works for
 * file:// uris, https urls and the `remote::path` form the git connector uses. */
export function folderOf(uri: string): string {
  const cut = Math.max(uri.lastIndexOf("/"), uri.lastIndexOf("\\"));
  return cut > 0 ? uri.slice(0, cut + 1) : "";
}

// A Confluence page's uri is `{base}/spaces/{KEY}/pages/{id}/{title}` (the page id makes
// every page's own path segment unique), so plain folderOf() gives each page its own
// one-document "folder" — no space grouping at all (reported live: "for all the pages
// under AR there's 1 individual entry with no parent as space"). Grouping by the prefix
// through the space key instead — still a REAL uri prefix, so it stays valid as the
// `uri_prefix` a folder-level label is stored against — fixes it with no backend change.
const CONFLUENCE_SPACE_RE = /^(.*\/spaces\/[^/]+\/)/;

function confluenceSpacePrefix(uri: string): string | null {
  return CONFLUENCE_SPACE_RE.exec(uri)?.[1] ?? null;
}

/** The short, human label for a group key — a bare space key ("AR") instead of the full
 * uri prefix for Confluence, the uri prefix as-is for everything else. */
export function groupLabel(folder: string, sourceType?: string): string {
  if (sourceType === "confluence") {
    const key = /\/spaces\/([^/]+)\/$/.exec(folder)?.[1];
    if (key) return `Space: ${decodeURIComponent(key)}`;
  }
  return folder || "(root)";
}

export function shortName(doc: IngestedDoc): string {
  if (doc.title) return doc.title;
  const parts = doc.uri.split(/[/\\]/);
  return parts[parts.length - 1] || doc.uri;
}

/** Documents grouped by folder, folders in path order — the shape the table renders and
 * the reason folder-level labelling has somewhere to live. `sourceType === "confluence"`
 * groups by space instead of by the (per-page-unique) path. */
export function groupByFolder(
  docs: IngestedDoc[],
  sourceType?: string,
): { folder: string; docs: IngestedDoc[] }[] {
  const keyOf =
    sourceType === "confluence"
      ? (uri: string) => confluenceSpacePrefix(uri) ?? folderOf(uri)
      : folderOf;
  const map = new Map<string, IngestedDoc[]>();
  for (const d of docs) {
    const key = keyOf(d.uri);
    (map.get(key) ?? map.set(key, []).get(key)!).push(d);
  }
  return [...map.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([folder, list]) => ({ folder, docs: list }));
}

// -- Epic/Feature/Story/Task tree (Azure DevOps + Jira) ----------------------------------
//
// Built ENTIRELY client-side from each document's own `metadata.id`/`metadata.parent_id`
// (stamped by the connector — see azure_devops.py's work_item_document / jira.py's
// issue_document) rather than by querying the knowledge-graph edges table: the graph edges
// exist for the AGENT's reasoning (multi-hop questions), this tree is a simpler, separate
// read of the same hierarchy for display. A doc whose parent_id doesn't resolve to another
// doc IN THIS SOURCE becomes a root — covers a real Epic (no parent) and an orphan (parent
// outside the connector's walk-up depth bound, or in another project) without
// special-casing either; orphans are just flagged rather than silently passing as a
// top-level Epic. `id`/`parent_id` are `string | number` because Azure DevOps ids are
// numeric but Jira KEYS are strings ("PROJ-123") — everything below keys by `String(id)`
// uniformly so both connectors share this one implementation.
const WORK_ITEM_TREE_SOURCE_TYPES = new Set(["azure_devops", "jira"]);

interface WorkItemMeta {
  id?: string | number;
  work_item_type?: string;
  state?: string;
  team?: string;
  sprint?: string;
  changed_date?: string;
  closed_date?: string;
  parent_id?: string | number | null;
  assigned_to?: string;
  tags?: string[];
}

function workItemMetaOf(doc: IngestedDoc): WorkItemMeta {
  const m = (doc.metadata ?? {}) as Record<string, unknown>;
  const idLike = (v: unknown) => (typeof v === "number" || typeof v === "string" ? v : undefined);
  return {
    id: idLike(m.id),
    work_item_type: typeof m.work_item_type === "string" ? m.work_item_type : undefined,
    state: typeof m.state === "string" ? m.state : undefined,
    team: typeof m.team === "string" ? m.team : undefined,
    sprint: typeof m.sprint === "string" ? m.sprint : undefined,
    changed_date: typeof m.changed_date === "string" ? m.changed_date : undefined,
    closed_date: typeof m.closed_date === "string" ? m.closed_date : undefined,
    parent_id: idLike(m.parent_id) ?? null,
    assigned_to: typeof m.assigned_to === "string" ? m.assigned_to : undefined,
    tags: Array.isArray(m.tags) ? m.tags.filter((t): t is string => typeof t === "string") : undefined,
  };
}

// Covers ADO's common process-template state names AND Jira's (Done/Resolved/Closed are
// standard Jira terminal statuses too) — no extra API call either way. A heavily
// customized workflow with unusual state names can misclassify an item here; this only
// ever affects display ordering, never grounding.
const COMPLETED_STATE_NAMES = new Set(["closed", "done", "resolved", "removed", "completed"]);

export function isCompleted(state?: string): boolean {
  return COMPLETED_STATE_NAMES.has((state ?? "").trim().toLowerCase());
}

export interface WorkItemNode {
  doc: IngestedDoc;
  meta: WorkItemMeta;
  children: WorkItemNode[];
  orphaned: boolean;
}

/** Not-yet-completed siblings first (most recently updated first), then completed siblings
 * (most recently completed first — closed date, falling back to last-updated when a
 * template doesn't populate it). Applied at every level of the tree. */
export function sortSiblings(nodes: WorkItemNode[]): WorkItemNode[] {
  const open = nodes.filter((n) => !isCompleted(n.meta.state));
  const done = nodes.filter((n) => isCompleted(n.meta.state));
  open.sort((a, b) => (b.meta.changed_date ?? "").localeCompare(a.meta.changed_date ?? ""));
  done.sort((a, b) =>
    (b.meta.closed_date || b.meta.changed_date || "").localeCompare(
      a.meta.closed_date || a.meta.changed_date || "",
    ),
  );
  return [...open, ...done];
}

function sortTree(nodes: WorkItemNode[]): WorkItemNode[] {
  const sorted = sortSiblings(nodes);
  for (const n of sorted) n.children = sortTree(n.children);
  return sorted;
}

export function buildWorkItemTree(docs: IngestedDoc[]): WorkItemNode[] {
  const byId = new Map<string, IngestedDoc>();
  for (const d of docs) {
    const id = workItemMetaOf(d).id;
    if (id != null) byId.set(String(id), d);
  }
  const nodeOf = new Map<string, WorkItemNode>();
  const nodeFor = (id: string): WorkItemNode => {
    let n = nodeOf.get(id);
    if (!n) {
      const doc = byId.get(id)!;
      n = { doc, meta: workItemMetaOf(doc), children: [], orphaned: false };
      nodeOf.set(id, n);
    }
    return n;
  };
  // No cycle guard needed: rendering only ever walks from `roots` outward, and a node that
  // participates in a (pathological, shouldn't-happen) parent cycle can never itself
  // qualify as a root — so a cycle is simply unreachable from the render root, not a stack
  // overflow risk. `collectAncestorIds` (used by search filtering) walks the other
  // direction and DOES carry its own bounded guard.
  const roots: WorkItemNode[] = [];
  for (const d of docs) {
    const meta = workItemMetaOf(d);
    if (meta.id == null) continue;
    const node = nodeFor(String(meta.id));
    const pid = meta.parent_id != null ? String(meta.parent_id) : null;
    if (pid != null && byId.has(pid)) {
      nodeFor(pid).children.push(node);
    } else {
      node.orphaned = pid != null; // has a parent id, it just wasn't ingested
      roots.push(node);
    }
  }
  return sortTree(roots);
}

/** Every ancestor id of `id`, walking parent_id up through `byId` — used so a search match
 * deep in the tree keeps its ancestor chain visible instead of being pruned into isolation. */
function collectAncestorIds(id: string, byId: Map<string, IngestedDoc>): Set<string> {
  const ids = new Set<string>();
  let cur: string | null | undefined = id;
  let guard = 0;
  while (cur != null && guard++ < 50) {
    const doc = byId.get(cur);
    const pid = doc ? workItemMetaOf(doc).parent_id : null;
    const pidStr = pid != null ? String(pid) : null;
    if (pidStr == null || !byId.has(pidStr) || ids.has(pidStr)) break;
    ids.add(pidStr);
    cur = pidStr;
  }
  return ids;
}

function typeAccent(type?: string): string {
  const t = (type ?? "").toLowerCase();
  if (t === "epic") return "text-unknown"; // the design system's violet token
  if (t === "feature") return "text-gold";
  if (t === "bug") return "text-danger";
  return "text-accent"; // story / product backlog item / task / anything else
}

function LabelChip({
  label,
  inherited,
  onRemove,
}: {
  label: { kind: "tag" | "aka"; value: string };
  inherited?: boolean;
  onRemove?: () => void;
}) {
  return (
    <span
      title={
        inherited
          ? "Inherited from this document's folder or connector — removing it affects every document it covers"
          : label.kind === "aka"
            ? "An alternate name for this content"
            : "A tag you can scope questions to"
      }
      className={cn(
        "inline-flex max-w-[220px] items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.08em]",
        label.kind === "aka" ? "bg-gold-soft text-gold" : "bg-accent-soft text-accent",
        inherited && "opacity-70",
      )}
    >
      {label.kind === "aka" ? "aka" : <Tag size={9} />}
      <span className="truncate normal-case tracking-normal">{label.value}</span>
      {onRemove && (
        <button onClick={onRemove} aria-label={`Remove ${label.value}`} className="hover:text-ink">
          <X size={9} />
        </button>
      )}
    </span>
  );
}

/** Add a tag/aka at one granularity. Deliberately explicit about WHAT it will cover,
 * because "tag" means something different on a folder than on one file. */
function AddLabel({
  covers,
  onAdd,
}: {
  covers: string;
  onAdd: (kind: "tag" | "aka", value: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"tag" | "aka">("tag");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!value.trim()) return;
    setBusy(true);
    try {
      await onAdd(kind, value.trim());
      setValue("");
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        title={`Add a tag or alternate name covering ${covers}`}
        className="inline-flex items-center gap-1 rounded-full bg-fill px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.08em] text-faint hover:text-ink"
      >
        <Plus size={9} /> label
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <select
        value={kind}
        onChange={(e) => setKind(e.target.value as "tag" | "aka")}
        className="rounded-full bg-fill px-1.5 py-0.5 font-mono text-[9.5px] text-ink outline-none"
      >
        <option value="tag">tag</option>
        <option value="aka">aka</option>
      </select>
      <input
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") setOpen(false);
        }}
        placeholder={kind === "tag" ? "architecture" : "the Zix roadmap"}
        className="w-[150px] rounded-full bg-fill px-2 py-0.5 text-[11.5px] text-ink outline-none placeholder:text-faint"
      />
      <button
        onClick={submit}
        disabled={busy || !value.trim()}
        className="rounded-full bg-accent-soft px-2 py-0.5 font-mono text-[9.5px] uppercase text-accent disabled:opacity-40"
      >
        add
      </button>
      <button onClick={() => setOpen(false)} className="text-faint hover:text-ink">
        <X size={11} />
      </button>
    </span>
  );
}

/** Lazily fetched, cached-per-mount list of what's inside an ingested .zip — see
 * api.documentArchive. Fetched on first expand, not on every row render: most archives in
 * a source will never be opened, and the endpoint re-derives the list from chunk text. */
function ArchiveContents({ sourceId, docId }: { sourceId: string; docId: string }) {
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "error"; message: string }
    | { status: "ok"; members: string[]; notes: string[] }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .documentArchive(sourceId, docId)
      .then((r) => !cancelled && setState({ status: "ok", members: r.members, notes: r.notes }))
      .catch((e) => !cancelled && setState({ status: "error", message: String((e as Error).message) }));
    return () => {
      cancelled = true;
    };
  }, [sourceId, docId]);

  if (state.status === "loading") {
    return <p className="pl-8 text-[11px] text-faint">Reading archive contents…</p>;
  }
  if (state.status === "error") {
    return <p className="pl-8 text-[11px] text-danger">Could not read contents: {state.message}</p>;
  }
  if (!state.members.length && !state.notes.length) {
    return <p className="pl-8 text-[11px] text-faint">No readable files found inside.</p>;
  }
  return (
    <div className="flex flex-col gap-1 pb-2 pl-8 pr-3">
      {state.members.map((name) => (
        <div key={name} className="flex items-center gap-1.5 text-[11.5px] text-muted">
          <FileText size={11} className="flex-shrink-0 text-faint" />
          <span className="truncate" title={name}>
            {name}
          </span>
        </div>
      ))}
      {state.notes.length > 0 && (
        <div className="mt-1 border-t border-fill2 pt-1">
          {state.notes.map((note) => (
            <p key={note} className="truncate text-[10.5px] text-faint" title={note}>
              {note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/** One row of the Epic/Feature/Story/Task tree, recursively rendering its own children when
 * expanded. Indentation (not nested boxes) carries depth — reads cleanly at arbitrary depth,
 * unlike the folder view's nested `bg-fill` blocks. */
function WorkItemRow({
  node,
  depth,
  sourceId,
  openNodes,
  toggle,
  add,
  remove,
}: {
  node: WorkItemNode;
  depth: number;
  sourceId: string;
  openNodes: Set<string>;
  toggle: (id: string) => void;
  add: (uriPrefix: string) => (kind: "tag" | "aka", value: string) => Promise<void>;
  remove: (l: DocLabel) => () => Promise<void>;
}) {
  const id = node.meta.id != null ? String(node.meta.id) : undefined;
  const hasChildren = node.children.length > 0;
  const open = hasChildren && id != null && openNodes.has(id);
  const done = isCompleted(node.meta.state);
  return (
    <>
      <div
        className="flex flex-wrap items-center gap-1.5 border-b border-fill2/50 py-2 pr-3 last:border-0"
        style={{ paddingLeft: 12 + depth * 20 }}
      >
        {hasChildren ? (
          <button
            onClick={() => id != null && toggle(id)}
            className="flex-shrink-0 text-faint hover:text-ink"
            aria-label={open ? "Collapse" : "Expand"}
          >
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
        ) : (
          <span className="w-3 flex-shrink-0" />
        )}
        <span className={cn("font-mono text-[9.5px] uppercase tracking-[0.06em]", typeAccent(node.meta.work_item_type))}>
          {node.meta.work_item_type || "Item"}
        </span>
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.04em]",
            done ? "bg-fill2 text-faint" : "bg-accent-soft text-accent",
          )}
        >
          {node.meta.state || "?"}
        </span>
        <span title={node.doc.uri} className="min-w-0 max-w-[280px] truncate text-[12.5px] text-ink">
          {shortName(node.doc)}
        </span>
        {node.meta.team && (
          <span className="font-mono text-[9.5px] text-faint" title="Team">
            {node.meta.team}
          </span>
        )}
        {node.meta.sprint && (
          <span className="font-mono text-[9.5px] text-faint" title="Sprint / iteration">
            · {node.meta.sprint}
          </span>
        )}
        {node.orphaned && (
          <span
            title="Its parent work item wasn't ingested (outside the sync's hierarchy walk, or in another project) — shown at top level rather than misrepresented as one."
            className="font-mono text-[9px] uppercase tracking-[0.06em] text-gold"
          >
            parent not ingested
          </span>
        )}
        {node.doc.labels.map((l) => (
          <LabelChip
            key={`${l.kind}-${l.value}-${l.uri_prefix}`}
            label={l}
            inherited={l.uri_prefix !== node.doc.uri}
            onRemove={
              l.uri_prefix === node.doc.uri
                ? remove({ source_id: sourceId, uri_prefix: node.doc.uri, kind: l.kind, value: l.value })
                : undefined
            }
          />
        ))}
        <span onClick={(e) => e.stopPropagation()}>
          <AddLabel covers="just this work item" onAdd={add(node.doc.uri)} />
        </span>
      </div>
      {open &&
        node.children.map((child) => (
          <WorkItemRow
            key={child.doc.doc_id}
            node={child}
            depth={depth + 1}
            sourceId={sourceId}
            openNodes={openNodes}
            toggle={toggle}
            add={add}
            remove={remove}
          />
        ))}
    </>
  );
}

function WorkItemTree({
  nodes,
  sourceId,
  openNodes,
  toggle,
  add,
  remove,
}: {
  nodes: WorkItemNode[];
  sourceId: string;
  openNodes: Set<string>;
  toggle: (id: string) => void;
  add: (uriPrefix: string) => (kind: "tag" | "aka", value: string) => Promise<void>;
  remove: (l: DocLabel) => () => Promise<void>;
}) {
  if (!nodes.length) return null;
  return (
    <div className="rounded-md bg-fill">
      {nodes.map((n) => (
        <WorkItemRow
          key={n.doc.doc_id}
          node={n}
          depth={0}
          sourceId={sourceId}
          openNodes={openNodes}
          toggle={toggle}
          add={add}
          remove={remove}
        />
      ))}
    </div>
  );
}

export function DocumentsModal({
  sourceId,
  sourceType,
  label,
  onClose,
  onLabelsChanged,
}: {
  sourceId: string;
  /** Drives grouping strategy — currently only "confluence" groups differently
   * (by space rather than by the per-page-unique path). */
  sourceType?: string;
  label: string;
  onClose: () => void;
  onLabelsChanged?: () => void;
}) {
  const hasWorkItemTree = WORK_ITEM_TREE_SOURCE_TYPES.has(sourceType ?? "");
  const [data, setData] = useState<SourceDocuments | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [openFolders, setOpenFolders] = useState<Set<string>>(new Set());
  const [expandedArchives, setExpandedArchives] = useState<Set<string>>(new Set());
  const [openNodes, setOpenNodes] = useState<Set<string>>(new Set());
  const toggleNode = (id: string) =>
    setOpenNodes((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  // Structured filters (Azure DevOps + Jira) — additional to (ANDed with) the free-text
  // search above. "" means unfiltered for that facet. Sprint stays blank for Jira (see
  // workItemMetaOf), so its dropdown simply doesn't render there — nothing extra to gate.
  const [sprintFilter, setSprintFilter] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.sourceDocuments(sourceId);
      setData(d);
      if (hasWorkItemTree) {
        // Few top-level Epics? Open them — collapsing a single group is pure friction.
        // Deeper levels start collapsed regardless (a real hierarchy can run deep).
        const roots = buildWorkItemTree(d.documents);
        if (roots.length <= 3) {
          setOpenNodes(new Set(roots.map((n) => n.meta.id).filter((id) => id != null).map(String)));
        }
      } else {
        // One folder? Open it — collapsing a single group is pure friction.
        const groups = groupByFolder(d.documents, sourceType);
        if (groups.length <= 3) setOpenFolders(new Set(groups.map((g) => g.folder)));
      }
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, [sourceId, sourceType, hasWorkItemTree]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const mutate = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
      onLabelsChanged?.();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const add = (uriPrefix: string) => (kind: "tag" | "aka", value: string) =>
    mutate(() => api.addLabel({ source_id: sourceId, uri_prefix: uriPrefix, kind, value }));
  const remove = (l: DocLabel) => () => mutate(() => api.removeLabel(l));

  const groups = useMemo(() => {
    const docs = (data?.documents ?? []).filter((d) => {
      if (!filter.trim()) return true;
      const q = filter.toLowerCase();
      return (
        shortName(d).toLowerCase().includes(q) ||
        d.uri.toLowerCase().includes(q) ||
        d.labels.some((l) => l.value.toLowerCase().includes(q))
      );
    });
    return groupByFolder(docs, sourceType);
  }, [data, filter, sourceType]);

  // Distinct real values present in this source's documents — never a hardcoded list, so
  // a filter can only ever offer something that actually exists to filter to.
  const workItemFilterOptions = useMemo(() => {
    if (!hasWorkItemTree) return { sprints: [], assignees: [], tags: [] };
    const sprints = new Set<string>();
    const assignees = new Set<string>();
    const tags = new Set<string>();
    for (const d of data?.documents ?? []) {
      const m = workItemMetaOf(d);
      if (m.sprint) sprints.add(m.sprint);
      if (m.assigned_to) assignees.add(m.assigned_to);
      for (const t of m.tags ?? []) tags.add(t);
    }
    return {
      sprints: [...sprints].sort(),
      assignees: [...assignees].sort(),
      tags: [...tags].sort(),
    };
  }, [data, hasWorkItemTree]);

  const workItemTree = useMemo(() => {
    if (!hasWorkItemTree) return [];
    const all = data?.documents ?? [];
    const anyFilterActive = Boolean(filter.trim() || sprintFilter || assigneeFilter || tagFilter);
    if (!anyFilterActive) return buildWorkItemTree(all);

    const q = filter.trim().toLowerCase();
    const matches = (d: IngestedDoc) => {
      const m = workItemMetaOf(d);
      if (sprintFilter && m.sprint !== sprintFilter) return false;
      if (assigneeFilter && m.assigned_to !== assigneeFilter) return false;
      if (tagFilter && !(m.tags ?? []).includes(tagFilter)) return false;
      if (q) {
        return (
          shortName(d).toLowerCase().includes(q) ||
          d.uri.toLowerCase().includes(q) ||
          d.labels.some((l) => l.value.toLowerCase().includes(q))
        );
      }
      return true;
    };

    // Keep every ANCESTOR of a match too, so a matched deep child doesn't get orphaned
    // from its visible context — a pruned tree, not a flat filtered list.
    const byId = new Map<string, IngestedDoc>();
    for (const d of all) {
      const id = workItemMetaOf(d).id;
      if (id != null) byId.set(String(id), d);
    }
    const keep = new Set<string>();
    for (const d of all) {
      const id = workItemMetaOf(d).id;
      if (id == null) continue;
      if (matches(d)) {
        keep.add(String(id));
        for (const a of collectAncestorIds(String(id), byId)) keep.add(a);
      }
    }
    return buildWorkItemTree(all.filter((d) => {
      const id = workItemMetaOf(d).id;
      return id != null && keep.has(String(id));
    }));
  }, [data, filter, hasWorkItemTree, sprintFilter, assigneeFilter, tagFilter]);

  const sourceLabels = (data?.labels ?? []).filter((l) => !l.uri_prefix);
  const total = data?.documents.length ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className="flex h-[80vh] w-[min(1000px,95vw)] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <div className="min-w-0">
            <div className="truncate text-[14.5px] font-semibold text-ink">{label}</div>
            <div className="font-mono text-[10.5px] text-faint">
              {total} document{total === 1 ? "" : "s"} learned
            </div>
          </div>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter by name, path or label…"
            className="ml-auto w-[240px] rounded-full bg-fill px-3 py-1.5 text-[12.5px] text-ink outline-none placeholder:text-faint"
          />
          <button
            onClick={onClose}
            aria-label="Close"
            className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-fill text-muted hover:text-ink"
          >
            <X size={13} />
          </button>
        </div>

        {hasWorkItemTree && (workItemFilterOptions.sprints.length > 0 || workItemFilterOptions.assignees.length > 0 || workItemFilterOptions.tags.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5 border-b border-fill2 px-5 py-2.5">
            <span className="font-mono text-[9.5px] uppercase tracking-[0.12em] text-faint">
              Filter:
            </span>
            {workItemFilterOptions.sprints.length > 0 && (
              <select
                value={sprintFilter}
                onChange={(e) => setSprintFilter(e.target.value)}
                className="rounded-full bg-fill px-2.5 py-1 text-[11.5px] text-ink outline-none"
              >
                <option value="">All iterations</option>
                {workItemFilterOptions.sprints.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            )}
            {workItemFilterOptions.assignees.length > 0 && (
              <select
                value={assigneeFilter}
                onChange={(e) => setAssigneeFilter(e.target.value)}
                className="rounded-full bg-fill px-2.5 py-1 text-[11.5px] text-ink outline-none"
              >
                <option value="">All assignees</option>
                {workItemFilterOptions.assignees.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            )}
            {workItemFilterOptions.tags.length > 0 && (
              <select
                value={tagFilter}
                onChange={(e) => setTagFilter(e.target.value)}
                className="rounded-full bg-fill px-2.5 py-1 text-[11.5px] text-ink outline-none"
              >
                <option value="">All tags</option>
                {workItemFilterOptions.tags.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            )}
            {(sprintFilter || assigneeFilter || tagFilter) && (
              <button
                onClick={() => {
                  setSprintFilter("");
                  setAssigneeFilter("");
                  setTagFilter("");
                }}
                className="font-mono text-[10.5px] uppercase tracking-[0.06em] text-faint hover:text-ink"
              >
                Clear
              </button>
            )}
          </div>
        )}

        {/* Connector-wide labels: these cover everything this source ever ingests. */}
        <div className="flex flex-wrap items-center gap-1.5 border-b border-fill2 px-5 py-2.5">
          <span className="font-mono text-[9.5px] uppercase tracking-[0.12em] text-faint">
            Whole connector:
          </span>
          {sourceLabels.map((l) => (
            <LabelChip key={`${l.kind}-${l.value}`} label={l} onRemove={remove(l)} />
          ))}
          <AddLabel covers="every document in this connector" onAdd={add("")} />
        </div>

        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-3">
          {error && (
            <div className="mb-3 rounded-md bg-danger-soft px-3 py-2 font-mono text-[11px] text-danger">
              {error}
            </div>
          )}
          {data && total === 0 && (
            <p className="py-8 text-center text-[13px] text-muted">
              Nothing ingested yet. Sync this connector, or use <code>/ingest &lt;path&gt;</code>.
            </p>
          )}
          {hasWorkItemTree ? (
            <WorkItemTree
              nodes={workItemTree}
              sourceId={sourceId}
              openNodes={openNodes}
              toggle={toggleNode}
              add={add}
              remove={remove}
            />
          ) : (
          groups.map((group) => {
            const open = openFolders.has(group.folder) || !!filter.trim();
            const folderLabels = (data?.labels ?? []).filter(
              (l) => l.uri_prefix && l.uri_prefix === group.folder,
            );
            return (
              <div key={group.folder} className="mb-2 rounded-md bg-fill">
                <div className="flex flex-wrap items-center gap-1.5 px-3 py-2">
                  <button
                    onClick={() =>
                      setOpenFolders((s) => {
                        const next = new Set(s);
                        next.has(group.folder) ? next.delete(group.folder) : next.add(group.folder);
                        return next;
                      })
                    }
                    className="flex min-w-0 items-center gap-1.5 text-left text-muted hover:text-ink"
                  >
                    {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    <Folder size={12} className="flex-shrink-0" />
                    <span className="truncate font-mono text-[11px]" title={group.folder || undefined}>
                      {groupLabel(group.folder, sourceType)}
                    </span>
                    <span className="ml-1 font-mono text-[9.5px] text-faint">
                      {group.docs.length}
                    </span>
                  </button>
                  {folderLabels.map((l) => (
                    <LabelChip key={`${l.kind}-${l.value}`} label={l} onRemove={remove(l)} />
                  ))}
                  {group.folder && (
                    <AddLabel
                      covers="this folder — including files ingested into it later"
                      onAdd={add(group.folder)}
                    />
                  )}
                </div>
                {open && (
                  <div className="border-t border-fill2">
                    {group.docs.map((doc) => {
                      const archive = isArchive(doc);
                      const expanded = archive && expandedArchives.has(doc.doc_id);
                      return (
                        <div key={doc.doc_id} className="border-b border-fill2/50 last:border-0">
                          <div
                            className={cn(
                              "flex flex-wrap items-center gap-1.5 px-3 py-2",
                              archive && "cursor-pointer",
                            )}
                            onClick={
                              archive
                                ? () =>
                                    setExpandedArchives((s) => {
                                      const next = new Set(s);
                                      next.has(doc.doc_id) ? next.delete(doc.doc_id) : next.add(doc.doc_id);
                                      return next;
                                    })
                                : undefined
                            }
                          >
                            {archive ? (
                              expanded ? <ChevronDown size={12} className="flex-shrink-0 text-faint" />
                                : <ChevronRight size={12} className="flex-shrink-0 text-faint" />
                            ) : (
                              <span className="w-3 flex-shrink-0" />
                            )}
                            {archive ? (
                              <FileArchive size={12} className="flex-shrink-0 text-faint" />
                            ) : (
                              <FileText size={12} className="flex-shrink-0 text-faint" />
                            )}
                            <span
                              title={doc.uri}
                              className="min-w-0 max-w-[300px] truncate text-[12.5px] text-ink"
                            >
                              {shortName(doc)}
                            </span>
                            <span className="font-mono text-[9.5px] text-faint">
                              {doc.chunks} chunk{doc.chunks === 1 ? "" : "s"}
                            </span>
                            {doc.labels.map((l) => (
                              <LabelChip
                                key={`${l.kind}-${l.value}-${l.uri_prefix}`}
                                label={l}
                                inherited={l.uri_prefix !== doc.uri}
                                onRemove={
                                  l.uri_prefix === doc.uri
                                    ? remove({ source_id: sourceId, uri_prefix: doc.uri, kind: l.kind, value: l.value })
                                    : undefined
                                }
                              />
                            ))}
                            {/* stopPropagation: AddLabel opens its own inline form — a click there
                                shouldn't also toggle the archive row it sits inside. */}
                            <span onClick={(e) => e.stopPropagation()}>
                              <AddLabel covers="just this document" onAdd={add(doc.uri)} />
                            </span>
                          </div>
                          {expanded && <ArchiveContents sourceId={sourceId} docId={doc.doc_id} />}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })
          )}
        </div>
      </div>
    </div>
  );
}
