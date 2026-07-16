"""Built-in agent tools: search_memory, remember, list_sources."""

from __future__ import annotations

import hashlib
import re

from quickjoiner.config import GapsConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import AgentTool, ToolSpec
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore

USER_TAUGHT_SOURCE = "notes:user-taught"


def teach_fact(catalog: Catalog, pipeline: IngestPipeline, fact: str, topic: str | None = None) -> str:
    """Store one user-taught fact as a note document. Shared by the agent's
    `remember` tool, `qj learn "<free text>"`, and POST /api/learn."""
    catalog.upsert_source(USER_TAUGHT_SOURCE, "User-taught notes", "notes")
    slug = re.sub(r"[^a-z0-9]+", "-", (topic or fact[:40]).lower()).strip("-") or "note"
    # Content-derived (not wall-clock) suffix: keeps the URI stable + unique so the
    # same fact is idempotent, and — since contextual chunking embeds the URI in each
    # chunk's breadcrumb — avoids folding a volatile timestamp token into the vector
    # (which perturbed retrieval near the grounding threshold).
    digest = hashlib.sha256(f"{topic or ''}\n{fact}".encode("utf-8")).hexdigest()[:10]
    doc = Document(
        uri=f"note://{slug}-{digest}",
        title=topic or f"Note: {fact[:60]}",
        text=fact,
        kind="note",
    )
    stats = pipeline.ingest([doc], USER_TAUGHT_SOURCE)
    return f"Remembered ({stats.summary()})."


def build_builtin_tools(
    store: KnowledgeStore,
    catalog: Catalog,
    pipeline: IngestPipeline,
    retrieval: RetrievalConfig,
    gaps: GapsConfig | None = None,
) -> list[AgentTool]:
    def _capture_gap(query: str) -> None:
        """Log a refusal as a knowledge gap. Fire-and-forget: any failure here must
        never change what search_memory returns to the agent."""
        if not (gaps and gaps.enabled):
            return
        try:
            near = store.search(query, top_k=3, min_score=0.0)
            nearest = [
                {"source_id": h.source_id, "title": h.title, "score": round(h.score, 4)}
                for h in near
            ]
            best = near[0].score if near else 0.0
            catalog.log_gap(query, best, nearest, store_query=gaps.store_queries)
        except Exception:
            pass

    def _graph_expansion(hits) -> str:
        """Related documents one knowledge-graph hop from the grounded hits — the
        multi-hop / cross-source channel. Only runs when there ARE grounded hits, so
        it never affects the grounded-vs-refuse decision; it adds leads to follow/cite."""
        if not retrieval.graph_expansion:
            return ""
        try:
            related = catalog.graph_expand(
                list({h.doc_id for h in hits}), retrieval.graph_expansion_limit
            )
        except Exception:
            return ""
        if not related:
            return ""
        lines = ["RELATED via knowledge graph (linked evidence the search may have missed "
                 "— follow up or cite as relevant):"]
        for r in related:
            label = r.get("title") or r.get("uri") or r["doc_id"]
            lines.append(
                f"- [source: {label} | uri: {r.get('uri', '')}] "
                f"— {r.get('src_name', '?')} {r['rel']} {r.get('dst_name', '?')}"
            )
        return "\n".join(lines)

    def search_memory(query: str, top_k: int | None = None) -> str:
        hits = store.search(query, top_k=top_k or retrieval.top_k, min_score=retrieval.min_score)
        if not hits:
            _capture_gap(query)
            return (
                "NO_RESULTS: nothing relevant found in learned memory for this query. "
                "Try a rephrased query, or tell the user you haven't learned this yet."
            )
        parts = []
        for h in hits:
            label = h.title or h.uri
            parts.append(
                f"[source: {label} | uri: {h.uri} | kind: {h.kind} | score: {h.score:.2f}]\n{h.text}"
            )
        expansion = _graph_expansion(hits)
        if expansion:
            parts.append(expansion)
        return "\n\n---\n\n".join(parts)

    def list_gaps() -> str:
        if not (gaps and gaps.enabled):
            return "Knowledge-gap tracking is disabled for this workspace."
        rows = catalog.list_gaps("open")
        if not rows:
            return "No open knowledge gaps — nothing has been refused yet."
        from quickjoiner.gaps import cluster_gaps

        clusters = cluster_gaps(rows, store.embedder, gaps.cluster_threshold, catalog)
        lines = [f"{len(rows)} open knowledge gap(s) in {len(clusters)} cluster(s):"]
        for c in clusters[:10]:
            sug = ", ".join(c["suggested_connectors"]) or "no obvious connector"
            lines.append(f"- [{c['count']}x] {c['label']} -> suggested: {sug}")
        lines.append(
            "These are questions users asked that memory could not answer. Connect the "
            "suggested source or teach the answer with the remember tool to close them."
        )
        return "\n".join(lines)

    def remember(fact: str, topic: str | None = None) -> str:
        return teach_fact(catalog, pipeline, fact, topic)

    def list_sources() -> str:
        sources = catalog.list_sources()
        if not sources:
            return "No sources connected yet."
        lines = [
            f"- {s['id']} (type={s['type']}, documents={s['doc_count']})" for s in sources
        ]
        return "Connected sources:\n" + "\n".join(lines)

    # A hub entity (a repo touching thousands of `defines`/`imports` edges, say) can have
    # far more neighbors than is useful — or safe — to hand an LLM in one tool result;
    # an uncapped dump has been observed to blow past the model's context/request limits
    # outright (a 500 from the provider, not a graceful truncation). Group by relation and
    # sample instead of listing exhaustively once past a threshold: still genuinely useful
    # (a relation-type breakdown is what you'd want from a hub anyway), always bounded.
    _NEIGHBORS_FULL_LIST_MAX = 60
    _NEIGHBORS_SAMPLE_PER_REL = 8

    def _format_edge(r) -> str:
        src = r["src_name"] or r["src"]
        dst = r["dst_name"] or r["dst"]
        detail = f" ({r['detail']})" if r["detail"] else ""
        evidence = r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"]
        return f"- {src} --{r['rel']}--> {dst}{detail} [evidence: {evidence}]"

    def graph_neighbors(entity: str) -> str:
        ent = catalog.resolve_entity(entity)
        if ent is None:
            return (
                f"NO_RESULTS: nothing named {entity!r} in the knowledge graph. "
                "Try the exact repo/package name or a known alias, or use search_memory."
            )
        rows = catalog.graph_neighbors(ent["id"])
        if not rows:
            return (f"{ent['name']} ({ent['type']}) is known but has no recorded "
                    "relationships yet. Use search_memory for unstructured facts.")

        if len(rows) <= _NEIGHBORS_FULL_LIST_MAX:
            lines = [f"{ent['name']} ({ent['type']}) — {len(rows)} relationship(s):"]
            lines.extend(_format_edge(r) for r in rows)
        else:
            by_rel: dict[str, list] = {}
            for r in rows:
                by_rel.setdefault(r["rel"], []).append(r)
            lines = [
                f"{ent['name']} ({ent['type']}) has {len(rows)} relationships — a hub, too "
                f"many to list in full. Showing up to {_NEIGHBORS_SAMPLE_PER_REL} examples per "
                "relation type below; if you're checking a specific other entity, use "
                "graph_path(a, b) instead — it returns just the connecting chain, not everything."
            ]
            for rel, group in sorted(by_rel.items(), key=lambda kv: -len(kv[1])):
                lines.append(f"\n{rel} ({len(group)} total):")
                lines.extend(_format_edge(r) for r in group[:_NEIGHBORS_SAMPLE_PER_REL])
                if len(group) > _NEIGHBORS_SAMPLE_PER_REL:
                    lines.append(f"  …and {len(group) - _NEIGHBORS_SAMPLE_PER_REL} more not shown.")
        lines.append(
            "Cite the evidence documents; call search_memory on them for the underlying text."
        )
        return "\n".join(lines)

    # 5, not 3: matches the web UI's Path Finder default. A repo's own
    # dependency-map/triple layers now routinely add an indirection hop or two
    # (repo -> project -> service -> service, say) before reaching a genuinely
    # cross-source relationship, so 3 was clipping real, evidenced chains.
    _DEFAULT_MAX_HOPS = 5

    def graph_path(a: str, b: str, max_hops: int = _DEFAULT_MAX_HOPS) -> str:
        ent_a, ent_b = catalog.resolve_entity(a), catalog.resolve_entity(b)
        for raw, ent in ((a, ent_a), (b, ent_b)):
            if ent is None:
                return (f"NO_RESULTS: nothing named {raw!r} in the knowledge graph. "
                        "Try the exact repo/package/ticket name, or search_memory.")
        if ent_a["id"] == ent_b["id"]:
            return f"{ent_a['name']} and {b!r} resolve to the same entity ({ent_a['id']})."
        path = catalog.graph_path(ent_a["id"], ent_b["id"], max_hops)
        if path is None:
            retry_hint = (
                f" Try again with a higher max_hops before concluding that — {max_hops} may "
                "have been too shallow." if max_hops < 8 else ""
            )
            return (f"NO_PATH: no recorded chain between {ent_a['name']} and {ent_b['name']} "
                    f"within {max_hops} hops.{retry_hint} That may only mean the link isn't "
                    "learned yet — try search_memory before concluding they are unrelated.")
        lines = [f"Path from {ent_a['name']} to {ent_b['name']} ({len(path)} hop(s)):"]
        for i, r in enumerate(path, 1):
            src = r["src_name"] or r["src"]
            dst = r["dst_name"] or r["dst"]
            detail = f" ({r['detail']})" if r["detail"] else ""
            evidence = r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"]
            lines.append(f"{i}. {src} --{r['rel']}--> {dst}{detail} [evidence: {evidence}]")
        lines.append("Cite the evidence documents for each hop you rely on.")
        return "\n".join(lines)

    return [
        AgentTool(
            spec=ToolSpec(
                name="search_memory",
                description=(
                    "Search everything QuickJoiner has learned about this organization "
                    "(code, docs, tickets, pipelines, notes). Call this BEFORE answering any "
                    "org-specific question. Returns relevant chunks with source citations, "
                    "or NO_RESULTS if nothing relevant has been learned."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query"},
                        "top_k": {"type": "integer", "description": "Max results (default from config)"},
                    },
                    "required": ["query"],
                },
            ),
            fn=search_memory,
        ),
        AgentTool(
            spec=ToolSpec(
                name="remember",
                description=(
                    "Permanently store a fact the user taught you about the organization "
                    "(process, convention, person, decision) so future questions can use it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string", "description": "The fact to remember, self-contained"},
                        "topic": {"type": "string", "description": "Short topic label"},
                    },
                    "required": ["fact"],
                },
            ),
            fn=remember,
        ),
        AgentTool(
            spec=ToolSpec(
                name="list_sources",
                description="List the data sources currently connected and how many documents each contributed.",
                input_schema={"type": "object", "properties": {}},
            ),
            fn=list_sources,
        ),
        AgentTool(
            spec=ToolSpec(
                name="list_gaps",
                description=(
                    "List the open knowledge gaps — questions users asked that memory "
                    "could not answer, clustered by topic with suggested connectors to "
                    "fix each. Use when the user asks what you don't know yet, what "
                    "should be connected, or where the knowledge holes are."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            fn=list_gaps,
        ),
        AgentTool(
            spec=ToolSpec(
                name="graph_neighbors",
                description=(
                    "Look up an entity (repo, package, project, ticket) in the knowledge "
                    "graph and list its recorded relationships (depends_on / provides / "
                    "references) with evidence documents. Resolves org shorthand aliases "
                    "(e.g. 'nautical models' for AppRiver.Nautical.Models). Use this FIRST "
                    "for questions about how projects/packages/tickets relate, then "
                    "search_memory on the evidence for details."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string",
                                   "description": "Entity name, alias, or id (e.g. repo:proj-a)"},
                    },
                    "required": ["entity"],
                },
            ),
            fn=graph_neighbors,
        ),
        AgentTool(
            spec=ToolSpec(
                name="graph_path",
                description=(
                    "Find the shortest recorded chain of relationships between two "
                    "entities (repos, packages, projects, tickets, services) in the "
                    "knowledge graph, with evidence per hop. Use for 'how is A related "
                    "to B?' questions. Resolves org shorthand aliases."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "string", "description": "First entity name/alias/id"},
                        "b": {"type": "string", "description": "Second entity name/alias/id"},
                        "max_hops": {"type": "integer",
                                     "description": "Search depth (default 5). Raise it and retry "
                                     "once before concluding NO_PATH means unrelated."},
                    },
                    "required": ["a", "b"],
                },
            ),
            fn=graph_path,
        ),
    ]
