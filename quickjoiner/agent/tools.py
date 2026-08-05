"""Built-in agent tools: search_memory, remember, list_sources."""

from __future__ import annotations

import hashlib
import re

from quickjoiner.agent.confidence import classify_evidence, score_chain, score_edge
from quickjoiner.config import GapsConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import AgentTool, ToolSpec
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.expansion import expand_query
from quickjoiner.memory.store import KnowledgeStore, SearchScope

USER_TAUGHT_SOURCE = "notes:user-taught"


def note_source(user: str | None, share: bool) -> tuple[str, str]:
    """Which note bucket a taught fact lands in: `(source_id, display name)`.

    A personal note is a **separate source**, not a flag on a document, because visibility is
    keyed on `documents.source_id -> sources.owner/shared` — so one private bucket per user
    gets filtering, graph scoping, cleanup and the connector plate for free, with no
    doc-schema change.

    Sharing is opt-in per the knowledge-scopes model: a wrong personal "fact" taught in
    passing must not become citable org truth for 200 people. Anonymous/open-mode teaching
    keeps the historic commons bucket, so single-user workspaces are unchanged and every
    note taught before this existed stays exactly as readable as it was.
    """
    if share or not user:
        return USER_TAUGHT_SOURCE, "User-taught notes"
    return f"notes:{user}", f"Notes from {user}"


def teach_fact(catalog: Catalog, pipeline: IngestPipeline, fact: str, topic: str | None = None,
               user: str | None = None, share: bool = False) -> str:
    """Store one user-taught fact as a note document. Shared by the agent's
    `remember` tool, `qj learn "<free text>"`, and POST /api/learn."""
    source_id, display = note_source(user, share)
    catalog.upsert_source(source_id, display, "notes")
    if source_id != USER_TAUGHT_SOURCE:
        # upsert_source deliberately preserves ownership (a sync must never reset a source
        # to commons), so a new personal bucket needs its owner set explicitly — otherwise
        # it would default to the ownerless commons and the note would be world-readable.
        catalog.set_source_ownership(source_id, owner=user, shared=False)
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
    stats = pipeline.ingest([doc], source_id)
    where = "shared with everyone" if source_id == USER_TAUGHT_SOURCE else "private to you"
    return f"Remembered — {where} ({stats.summary()})."


def build_builtin_tools(
    store: KnowledgeStore,
    catalog: Catalog,
    pipeline: IngestPipeline,
    retrieval: RetrievalConfig,
    gaps: GapsConfig | None = None,
    score_ledger: dict[str, float] | None = None,
    scope=None,
    user: str | None = None,
) -> list[AgentTool]:
    """`scope` (memory.store.SearchScope) narrows every memory read for this turn — to the
    connectors/documents the user picked, AND to what the acting user is allowed to read
    (knowledge scopes). Filtered inside the vector + FTS query, so a scoped question does
    strictly less work and can't drift onto unrelated — or unreadable — sources.

    The two dimensions are deliberately distinguished below: a refusal is worded differently
    when the USER narrowed the question (`has_picks`) than when visibility did, because the
    user chose the first and must not be told the second is their doing.
    """
    visible = None if scope is None else scope.visible_source_ids

    def _capture_gap(query: str) -> None:
        """Log a refusal as a knowledge gap. Fire-and-forget: any failure here must
        never change what search_memory returns to the agent.

        The near-miss probe carries the visibility filter but NOT the user's picks: a gap is
        a fact about what the org hasn't learned, so a document the asker simply didn't pick
        is still a legitimate near miss — while one they may not read must never surface its
        title here, since the gaps backlog is read by everyone.
        """
        if not (gaps and gaps.enabled):
            return
        try:
            near = store.search(query, top_k=3, min_score=0.0,
                                scope=SearchScope(visible_source_ids=visible)
                                if visible is not None else None)
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
                list({h.doc_id for h in hits}), retrieval.graph_expansion_limit,
                visible_source_ids=visible,
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
        search_q = query
        if retrieval.alias_expansion:
            try:
                search_q = expand_query(catalog, query)
            except Exception:  # expansion is best-effort; never break a search on it
                search_q = query
        hits = store.search(search_q, top_k=top_k or retrieval.top_k,
                            min_score=retrieval.min_score, scope=scope)
        if not hits:
            _capture_gap(query)  # log the user's ORIGINAL query as the gap, not the expanded one
            if scope is not None and scope.has_picks():
                # A scoped refusal is a different fact from a global one, and saying which is
                # the difference between "we never learned this" and "not in what you picked".
                return (
                    "NO_RESULTS: nothing relevant in the sources this question is scoped to. "
                    "Say so plainly and offer to look across all of memory — do NOT claim the "
                    "organisation has never learned it, because you only searched a slice."
                )
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

    def remember(fact: str, topic: str | None = None, share: bool = False) -> str:
        return teach_fact(catalog, pipeline, fact, topic, user=user, share=share)

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

    def _evidence_class(r) -> str:
        if r["rel"] == "same_as":
            # Identity bridges (ingest/bridges.py) carry no evidence doc — they are
            # name-equality inferences and score as their own lowest-tier class.
            return "name-bridge"
        return classify_evidence(r["evidence_title"] or "", r["evidence_uri"] or "",
                                 r["evidence_kind"] or "")

    def _hop_score(r) -> float:
        """Server-side confidence for one edge row: evidence shape + corroboration."""
        c = catalog.edge_corroboration(r["src"], r["rel"], r["dst"],
                                       visible_source_ids=visible)
        return score_edge(_evidence_class(r), c["doc_count"], c["source_count"])

    def _format_edge(r, flag_weak: bool = False) -> str:
        src = r["src_name"] or r["src"]
        dst = r["dst_name"] or r["dst"]
        detail = f" ({r['detail']})" if r["detail"] else ""
        # Bridges have no evidence doc; their self-describing detail is the whole story.
        evidence = (r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"]
                    or "none — name-equality inference")
        suffix = ""
        if flag_weak and _evidence_class(r) == "meeting-notes":
            suffix = " (low-confidence: meeting-notes evidence)"
        elif flag_weak and _evidence_class(r) == "name-bridge":
            suffix = " (low-confidence: same-name identity bridge, no document asserts it)"
        return f"- {src} --{r['rel']}--> {dst}{detail} [evidence: {evidence}]{suffix}"

    def graph_neighbors(entity: str) -> str:
        ent = catalog.resolve_entity(entity)
        if ent is None:
            return (
                f"NO_RESULTS: nothing named {entity!r} in the knowledge graph. "
                "Try the exact repo/package name or a known alias, or use search_memory."
            )
        rows = catalog.graph_neighbors(ent["id"], visible_source_ids=visible)
        if not rows:
            return (f"{ent['name']} ({ent['type']}) is known but has no recorded "
                    "relationships yet. Use search_memory for unstructured facts.")

        if len(rows) <= _NEIGHBORS_FULL_LIST_MAX:
            lines = [f"{ent['name']} ({ent['type']}) — {len(rows)} relationship(s):"]
            # flag_weak only on the full list: the hub branch stays cheap (no
            # corroboration queries there either — the flag is classification-only).
            lines.extend(_format_edge(r, flag_weak=True) for r in rows)
        else:
            # Grouped by relation AND DIRECTION. Grouping by relation alone silently hid
            # whole categories: a team with 39 outgoing `works_on` (its projects) and 15
            # incoming `works_on` (its people) put all 54 in one bucket ordered by dst, so
            # the 8 sampled rows were all projects and not one member appeared — the
            # question that motivated this. Incoming and outgoing are different facts and
            # each gets its own budget.
            by_rel: dict[tuple[str, bool], list] = {}
            for r in rows:
                by_rel.setdefault((r["rel"], r["src"] == ent["id"]), []).append(r)
            lines = [
                f"{ent['name']} ({ent['type']}) has {len(rows)} relationships — a hub, too "
                f"many to list in full. Showing up to {_NEIGHBORS_SAMPLE_PER_REL} examples per "
                "relation type below; for the COMPLETE list of one relation use "
                "graph_relations, and if you're checking a specific other entity use "
                "graph_path(a, b) — it returns just the connecting chain, not everything."
            ]
            for (rel, outgoing), group in sorted(by_rel.items(), key=lambda kv: -len(kv[1])):
                arrow = f"--{rel}-->" if outgoing else f"<--{rel}--"
                lines.append(f"\n{arrow} ({len(group)} total):")
                lines.extend(_format_edge(r) for r in group[:_NEIGHBORS_SAMPLE_PER_REL])
                if len(group) > _NEIGHBORS_SAMPLE_PER_REL:
                    lines.append(f"  …and {len(group) - _NEIGHBORS_SAMPLE_PER_REL} more not shown "
                                 f"— graph_relations('{rel}') lists them all.")
        lines.append(
            "Cite the evidence documents; call search_memory on them for the underlying text."
        )
        return "\n".join(lines)

    # One relation across the whole graph, grouped by its right-hand entity — the shape
    # "list every team with its members" needs. Bounded, and truncation is stated.
    _RELATIONS_LIMIT = 400

    def graph_relations(rel: str, src_type: str | None = None,
                        dst_type: str | None = None) -> str:
        rows = catalog.graph_relations(rel, src_type, dst_type, limit=_RELATIONS_LIMIT + 1,
                                       visible_source_ids=visible)
        if not rows:
            filters = ", ".join(
                f"{k}={v}" for k, v in (("src_type", src_type), ("dst_type", dst_type)) if v
            )
            return (f"NO_RESULTS: no {rel!r} relationships recorded"
                    + (f" for {filters}" if filters else "")
                    + ". Check the relation name, or use search_memory — the graph only "
                      "holds relationships an ingested document actually asserted.")
        truncated = len(rows) > _RELATIONS_LIMIT
        rows = rows[:_RELATIONS_LIMIT]

        grouped: dict[str, list] = {}
        for r in rows:
            grouped.setdefault(r["dst_name"] or r["dst"], []).append(r)
        head = f"{len(rows)} {rel!r} relationship(s)"
        if src_type or dst_type:
            head += f" ({src_type or 'any'} -> {dst_type or 'any'})"
        lines = [f"{head}, grouped by the thing on the right:"]
        for dst, group in grouped.items():
            names = sorted({(r["src_name"] or r["src"]) for r in group})
            evidence = sorted({(r["evidence_title"] or r["evidence_uri"] or "")
                               for r in group if r["evidence_title"] or r["evidence_uri"]})
            cite = f" [evidence: {'; '.join(evidence[:2])}]" if evidence else ""
            lines.append(f"\n{dst} ({len(names)}):{cite}")
            lines.append("  " + ", ".join(names))
        if truncated:
            lines.append(
                f"\n⚠ truncated at {_RELATIONS_LIMIT} relationships — there are more. Narrow "
                "with src_type/dst_type, or ask about specific entities, and TELL THE USER "
                "the list is partial."
            )
        lines.append("Cite the evidence documents for the groups you report.")
        return "\n".join(lines)

    # 5, not 3: matches the web UI's Path Finder default. A repo's own
    # dependency-map/triple layers now routinely add an indirection hop or two
    # (repo -> project -> service -> service, say) before reaching a genuinely
    # cross-source relationship, so 3 was clipping real, evidenced chains.
    _DEFAULT_MAX_HOPS = 5

    def _format_hop(i, r) -> str:
        src = r["src_name"] or r["src"]
        dst = r["dst_name"] or r["dst"]
        detail = f" ({r['detail']})" if r["detail"] else ""
        evidence = r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"]
        return f"{i}. {src} --{r['rel']}--> {dst}{detail} [evidence: {evidence}]"

    def graph_path(a: str, b: str, max_hops: int = _DEFAULT_MAX_HOPS) -> str:
        ent_a, ent_b = catalog.resolve_entity(a), catalog.resolve_entity(b)
        for raw, ent in ((a, ent_a), (b, ent_b)):
            if ent is None:
                return (f"NO_RESULTS: nothing named {raw!r} in the knowledge graph. "
                        "Try the exact repo/package/ticket name, or search_memory.")
        if ent_a["id"] == ent_b["id"]:
            return f"{ent_a['name']} and {b!r} resolve to the same entity ({ent_a['id']})."
        chains = catalog.graph_path_candidates(ent_a["id"], ent_b["id"], max_hops,
                                               max_candidates=3,
                                               visible_source_ids=visible)
        if not chains:
            retry_hint = (
                f" Try again with a higher max_hops before concluding that — {max_hops} may "
                "have been too shallow." if max_hops < 8 else ""
            )
            return (f"NO_PATH: no recorded chain between {ent_a['name']} and {ent_b['name']} "
                    f"within {max_hops} hops.{retry_hint} That may only mean the link isn't "
                    "learned yet — try search_memory before concluding they are unrelated.")
        def _weak_hop_caveats(chain, hop_scores) -> list[str]:
            out = []
            for i, (r, s) in enumerate(zip(chain, hop_scores), 1):
                if s < 0.40:
                    evidence = r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"]
                    out.append(f'   hop {i} is low-confidence ({s:.2f}): sourced only from '
                               f'informal meeting notes ("{evidence}") — corroborate before '
                               "relying on it.")
            return out

        def _record_ledger(chain, chain_score) -> None:
            """Per-request score ledger (plan 06 §C): remember the confidence computed
            for each evidence ref this turn, keyed the way the tool prints it, so a
            later candidates block can carry server-side numbers — never the LLM's."""
            if score_ledger is None:
                return
            for r in chain:
                ref = str(r["evidence_title"] or r["evidence_uri"] or r["evidence_doc_id"])
                key = ref.strip().lower()
                score_ledger[key] = max(score_ledger.get(key, 0.0), chain_score)

        if len(chains) == 1:
            path = chains[0]
            hop_scores = [_hop_score(r) for r in path]
            _record_ledger(path, score_chain(hop_scores))
            lines = [f"Path from {ent_a['name']} to {ent_b['name']} ({len(path)} hop(s)):"]
            lines.extend(_format_hop(i, r) for i, r in enumerate(path, 1))
            lines.extend(_weak_hop_caveats(path, hop_scores))  # empty when healthy: golden-safe
            lines.append("Cite the evidence documents for each hop you rely on.")
            return "\n".join(lines)
        # Multiple materially different recorded connections (different intermediates,
        # relations, or evidence shapes) — surface ALL of them and instruct the model
        # not to silently pick one. Same prompt-in-tool-text pattern as the hub cap.
        lines = [f"{len(chains)} distinct recorded connections exist between "
                 f"{ent_a['name']} and {ent_b['name']}:"]
        for k, chain in enumerate(chains, 1):
            hop_scores = [_hop_score(r) for r in chain]
            chain_score = score_chain(hop_scores)
            _record_ledger(chain, chain_score)
            lines.append(f"\nChain {k} ({len(chain)} hop(s), confidence {chain_score:.2f}):")
            lines.extend(_format_hop(i, r) for i, r in enumerate(chain, 1))
            lines.extend(_weak_hop_caveats(chain, hop_scores))
        lines.append(
            "\nThese chains are materially different (different intermediates / relations / "
            "evidence). State both with their evidence and their confidence, or ask ONE short "
            "clarifying question about which the user means — do NOT present only one as the "
            "answer. Cite the evidence documents for each hop you rely on."
        )
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
                    "(process, convention, person, decision) so future questions can use it. "
                    "Stored privately to this user by default; pass share=true ONLY when they "
                    "say it is for everyone, because a shared note becomes citable org truth "
                    "for every user of the workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string", "description": "The fact to remember, self-contained"},
                        "topic": {"type": "string", "description": "Short topic label"},
                        "share": {
                            "type": "boolean",
                            "description": (
                                "True only if the user explicitly wants everyone in the "
                                "workspace to be able to read and cite this. Default false."
                            ),
                        },
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
                    "Look up an entity (repo, package, project, ticket, topic, datastore) "
                    "in the knowledge graph and list its recorded relationships "
                    "(depends_on / provides / publishes_to / subscribes_to / stores_in / "
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
                name="graph_relations",
                description=(
                    "List EVERY recorded relationship of one kind across the whole "
                    "organization, grouped by the entity on the right — the tool for "
                    "'list all X with their Y' questions. Use this instead of repeating "
                    "search_memory per entity: 'all teams with their members' is "
                    "graph_relations('works_on', src_type='person', dst_type='team'); "
                    "'which team owns which repo' is graph_relations('owns', "
                    "src_type='team', dst_type='repo'); 'what is deployed where' is "
                    "graph_relations('deploys'). Relations: works_on, owns, part_of, "
                    "depends_on, provides, deploys, references, publishes_to, "
                    "subscribes_to, stores_in, implemented_in, builds, related_to. "
                    "Types: person, team, repo, service, project, package, environment, "
                    "ticket, pipeline, topic, datastore, branch, merge_request, symbol, "
                    "module. Says so explicitly if the list is truncated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "rel": {"type": "string",
                                "description": "Relation name, e.g. works_on"},
                        "src_type": {"type": "string",
                                     "description": "Optional entity type on the left"},
                        "dst_type": {"type": "string",
                                     "description": "Optional entity type on the right"},
                    },
                    "required": ["rel"],
                },
            ),
            fn=graph_relations,
        ),
        AgentTool(
            spec=ToolSpec(
                name="graph_path",
                description=(
                    "Find the recorded chain(s) of relationships between two "
                    "entities (repos, packages, projects, tickets, services) in the "
                    "knowledge graph, with evidence per hop. Use for 'how is A related "
                    "to B?' questions. Resolves org shorthand aliases. If multiple "
                    "distinct chains are recorded, the result lists all of them; "
                    "present or disambiguate per the system prompt."
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
