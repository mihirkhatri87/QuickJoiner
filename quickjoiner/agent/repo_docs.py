"""Generated architecture briefs for one repo ("AGENTS.md from the perspective of a
principal engineer/architect"): file tree + code-graph facts (defines/imports) +
dependency map + retrieved prose, synthesized by one LLM call into a markdown doc.

QuickJoiner-internal only: saved under <workspace>/generated/<source>/AGENTS.md and
re-ingested as a doc, never written into the repo's actual git working tree — this is
QuickJoiner's own evidence-grounded read of the repo, not the org's official docs.

If the repo already has a real AGENTS.md ingested, this is a REFINEMENT, not a
from-scratch rewrite: the existing doc becomes its own evidence block and the model is
instructed to preserve what's still true, using fresh evidence to correct/extend it —
the existing doc may carry human judgment (or a stronger model's) that the deterministic
evidence alone can't reconstruct.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from quickjoiner.connectors.base import Document
from quickjoiner.connectors.deps import SKIP_DIRS
from quickjoiner.ingest.pipeline import source_entity

_REPO_SOURCE_TYPES = ("git", "files")
MAX_TREE_DEPTH = 3
MAX_TREE_ENTRIES = 400
MAX_GRAPH_EDGES = 250
MAX_PROSE_CHUNKS = 20
MAX_CHUNK_CHARS = 1200
MAX_EXISTING_CHARS = 6000
MAX_DEPMAP_CHARS = 4000

AGENTS_MD_SYSTEM = """\
You write architecture onboarding briefs for software repositories. Your reader is a \
principal engineer/architect who joined the org this week. In a five-minute read they must \
learn: what this system is for, where its boundaries are, how it connects to other systems \
in the org, and what to verify with a teammate next.

You are given only static evidence about ONE repository, in labeled blocks:
- FILE TREE: real file paths from the repo.
- CODE GRAPH: regex-extracted facts — `defines <symbol>` and `imports <module>` per file. \
These are textual matches, NOT semantic analysis: an import proves a reference exists, \
never that something is called at runtime, and symbols may be dead code.
- DEPENDENCY MAP: parsed from package manifests — packages this repo PROVIDES and DEPENDS ON \
(with versions), plus org-internal alias names. This is the strongest evidence of \
cross-system connections.
- PROSE: excerpts from READMEs/wikis/docs, each with a citation URI. Prose states intent; \
it may be stale — where prose and code-level evidence disagree, say so.
You cannot run code, and you know nothing about this repo beyond these blocks.

EVIDENCE RULES — these override everything else:
1. Cite every non-trivial claim as [uri], using the uri printed in the evidence block header \
it came from. A sentence combining sources carries multiple citations.
2. Use ONLY the provided evidence. No background knowledge about this org, project, or any \
similarly-named technology — if the evidence surprises you, the evidence wins.
3. Inference is welcome but must be visibly anchored: write "X likely Y (inferred from Z) \
[uri]", never inference stated as fact. Choose verbs that match the evidence tier: \
"depends on" (manifest), "imports/references" (code graph), "intended to" (prose), \
"appears to" (file-tree layout). Never "calls", "at runtime", "in production" — you have \
no evidence at that tier.
4. Absence of evidence is a finding. If a section has no support, write exactly: \
"Not evidenced in available sources." and nothing else in it. Never fill a gap with \
plausible generic architecture — a wrong claim costs the reader more than a gap.
5. Everything the evidence leaves unsettled that a principal engineer would need goes in \
Open questions — phrased as concrete questions to ask a teammate, each noting the partial \
evidence that raised it.

WRITING RULES:
- Markdown, the exact skeleton below, under ~1000 words total. Lead every section with its \
takeaway; bullets over paragraphs.
- Name real paths, symbols, and package ids — never "various modules" or "helper utilities".
- Do NOT enumerate files or restate the tree. Synthesize what the pieces mean together: \
layering, dependency direction, coupling hot-spots, one-way boundaries, implicit design \
decisions the structure reveals.
- Prioritize org-internal connections (use the alias names) over third-party libraries; \
list well-known third-party frameworks only when they define the architecture (web \
framework, database driver, message bus client).
- Five load-bearing facts beat twenty trivial ones.

OUTPUT SKELETON (keep all headings; use rule 4 where empty):
# {repo} — Architecture Brief
> One sentence: purpose + primary stack. [cite]
## What this system is
## Boundaries & responsibilities
What it owns / does NOT own; who or what it serves.
## Key modules & entry points
Where the important logic lives and where execution starts.
## Connected systems
### Provides (what other systems get from this repo)
### Consumes (org-internal first, then architecture-defining third-party)
## Data & control flow
## Build, deployment & operations
## Open questions for your first week
_Generated from static evidence on {date}; verify before relying on runtime claims._\
"""

MERGE_ADDENDUM = """

This is a REFINEMENT task, not a from-scratch write. An EXISTING AGENTS.MD evidence block \
is included below the other evidence — it may have been written by a human or a more \
capable model and can contain real judgment the other evidence can't reconstruct. Preserve \
everything in it that remains true and useful. Use the fresh CODE GRAPH / DEPENDENCY MAP / \
PROSE evidence to: correct anything the existing doc gets wrong, add what it's missing, and \
update anything stale. Where the existing doc and fresh evidence conflict, trust the \
fresher, more concrete evidence (code graph/dependency map) over stale prose, but say so in \
Open Questions rather than silently dropping the old claim if you can't fully verify which \
is right. Output ONE merged document in the skeleton above — never two separate docs, never \
a diff/changelog.\
"""

AGENTS_MD_USER_TEMPLATE = """\
Repository: {repo_name}

{existing_block}=== EVIDENCE: FILE TREE (uri: {repo_uri}) ===
{file_tree}

=== EVIDENCE: CODE GRAPH — regex-level defines/imports (uri: {repo_uri}) ===
{code_graph_facts}

=== EVIDENCE: DEPENDENCY MAP — parsed manifests + org aliases (uri: {depmap_uri}) ===
{dependency_map}

{prose_blocks}

=== TASK ===
Write the architecture brief for {repo_name} now, following the system instructions \
exactly. Remember: cite [uri] on every non-trivial claim; where evidence is missing, say \
so instead of guessing.\
"""

EXISTING_BLOCK = """\
=== EVIDENCE: EXISTING AGENTS.MD (uri: {uri}) ===
{text}

"""

PROSE_BLOCK = """\
=== EVIDENCE: PROSE — {title} (uri: {uri}) ===
{text}\
"""

PROSE_QUERIES = (
    "{repo} architecture overview purpose",
    "{repo} how it works components modules",
    "{repo} dependencies integrations connects to",
    "{repo} deployment operations configuration",
)

NOT_A_REPO_SOURCE = (
    "{name!r} is not a configured git/files source. Configured repo-like sources: {options}"
)
NO_LOCAL_ROOT = (
    "No local clone/root found for {name!r} — sync it first (qj sync {name})."
)


def _find_repo_source(config, name: str):
    for source in config.sources:
        if source.name == name and source.type in _REPO_SOURCE_TYPES:
            return source
    return None


def _repo_root(workspace: Path, source) -> Path | None:
    if source.type == "git":
        root = workspace / "repos" / source.name
        return root if root.exists() else None
    if source.type == "files":
        target = source.options.get("path") or source.options.get("url") or ""
        if not target or target.startswith(("http://", "https://")):
            return None
        path = Path(target)
        return path if path.exists() else None
    return None


def _walk_tree(root: Path, max_depth: int = MAX_TREE_DEPTH, max_entries: int = MAX_TREE_ENTRIES) -> str:
    lines: list[str] = []
    base_depth = len(root.parts)
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        depth = len(path.parts) - base_depth
        if depth > (max_depth if path.is_dir() else max_depth + 1):
            continue
        rel = path.relative_to(root).as_posix()
        lines.append(rel + ("/" if path.is_dir() else ""))
        if len(lines) >= max_entries:
            lines.append("… (truncated)")
            break
    return "\n".join(lines) if lines else "(empty or unreadable)"


def _basename(uri: str) -> str:
    tail = uri.split("::")[-1].split("?")[0].split("#")[0].rstrip("/")
    return tail.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _is_agents_md(doc: dict) -> bool:
    return _basename(doc.get("uri") or "").lower() == "agents.md"


def _is_dependency_map(doc: dict) -> bool:
    return (doc.get("uri") or "").endswith("::dependency-map")


def _graph_facts(catalog, repo_entity_id: str, max_edges: int = MAX_GRAPH_EDGES) -> str:
    rows = catalog.graph_neighbors(repo_entity_id)
    lines = [
        f"{repo_entity_id} --{r['rel']}--> {r['dst']} ({r['detail']})"
        for r in rows
        if r["src"] == repo_entity_id and r["rel"] in ("defines", "imports")
    ]
    lines = lines[:max_edges]
    return "\n".join(lines) if lines else "(no code-graph facts extracted for this repo yet)"


def collect_repo_prose(store, retrieval, source_id: str, repo_name: str,
                        exclude_doc_ids: set[str]) -> list:
    """Union of per-query retrievals scoped (post-filtered) to one source — store.search
    has no source predicate, so a wide top_k is fetched and filtered down."""
    best: dict[tuple, object] = {}
    wide_top_k = max(retrieval.top_k * 4, 40)
    for template in PROSE_QUERIES:
        query = template.format(repo=repo_name)
        for hit in store.search(query, top_k=wide_top_k, min_score=retrieval.min_score):
            if hit.source_id != source_id or hit.doc_id in exclude_doc_ids:
                continue
            key = (hit.doc_id, hit.text[:80])
            if key not in best or hit.score > best[key].score:
                best[key] = hit
    ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
    return ranked[:MAX_PROSE_CHUNKS]


def _doc_text(store, doc: dict, max_chars: int) -> str:
    chunks = store.get_document_chunks(doc["doc_id"])
    return "\n".join(chunks)[:max_chars]


def generate_agents_md(
    ctx,
    source_name: str,
    provider=None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> tuple[str, Path]:
    """Generate (or refine) one repo's architecture brief. Returns (markdown, saved_path).

    Raises ValueError for a missing/unsynced source — these are user-facing usage errors,
    not "not learned yet" (that concept doesn't apply here: there's always at least a file
    tree once a repo is cloned).
    """
    source = _find_repo_source(ctx.config, source_name)
    if source is None:
        options = ", ".join(s.name for s in ctx.config.sources if s.type in _REPO_SOURCE_TYPES) or "(none configured)"
        raise ValueError(NOT_A_REPO_SOURCE.format(name=source_name, options=options))

    root = _repo_root(ctx.workspace, source)
    if root is None:
        raise ValueError(NO_LOCAL_ROOT.format(name=source_name))

    source_id = f"{source.type}:{source.name}"
    repo_entity_id, repo_display_name, _ = source_entity(source_id)

    docs = ctx.catalog.documents_for_source(source_id)
    existing = next((d for d in docs if _is_agents_md(d)), None)
    depmap = next((d for d in docs if _is_dependency_map(d)), None)

    exclude_ids = {d["doc_id"] for d in (existing, depmap) if d}
    prose_hits = collect_repo_prose(ctx.store, ctx.config.retrieval, source_id, repo_display_name, exclude_ids)

    file_tree = _walk_tree(root)
    graph_facts = _graph_facts(ctx.catalog, repo_entity_id)
    depmap_text = _doc_text(ctx.store, depmap, MAX_DEPMAP_CHARS) if depmap else "(no dependency manifest found)"
    depmap_uri = depmap["uri"] if depmap else source_id

    prose_blocks = "\n\n".join(
        PROSE_BLOCK.format(title=h.title or h.uri, uri=h.uri, text=h.text[:MAX_CHUNK_CHARS])
        for h in prose_hits
    ) or "(no additional prose evidence retrieved)"

    system = AGENTS_MD_SYSTEM
    existing_block = ""
    if existing:
        system = system + MERGE_ADDENDUM
        existing_text = _doc_text(ctx.store, existing, MAX_EXISTING_CHARS)
        existing_block = EXISTING_BLOCK.format(uri=existing["uri"], text=existing_text)

    user_msg = AGENTS_MD_USER_TEMPLATE.format(
        repo_name=repo_display_name,
        repo_uri=source_id,
        existing_block=existing_block,
        file_tree=file_tree,
        code_graph_facts=graph_facts,
        depmap_uri=depmap_uri,
        dependency_map=depmap_text,
        prose_blocks=prose_blocks,
    )

    if provider is None:
        provider = ctx.build_provider(provider_override, model_override)

    result = provider.chat([{"role": "user", "content": user_msg}], system=system)

    today = datetime.now(timezone.utc).date().isoformat()
    origin_note = (
        "Refined from an existing AGENTS.md found in this repo."
        if existing
        else "No existing AGENTS.md was found in this repo — generated from scratch."
    )
    banner = (
        f"> **QuickJoiner-generated architecture brief for `{repo_display_name}`** — not the "
        f"org's official docs; synthesized from static evidence ({today}), verify before "
        f"relying on any runtime claim. {origin_note}\n\n"
    )
    markdown = banner + result.text.strip() + "\n"

    out_dir = ctx.workspace / "generated" / source.name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "AGENTS.md"
    path.write_text(markdown, encoding="utf-8")

    ctx.catalog.upsert_source("generated:agents-md", "Generated architecture briefs", "generated", {})
    ctx.pipeline.ingest(
        [
            Document(
                uri=f"agents-md://{source.name}",
                title=f"{repo_display_name} — Architecture Brief (generated)",
                text=markdown,
                kind="note",
            )
        ],
        "generated:agents-md",
    )
    return markdown, path
