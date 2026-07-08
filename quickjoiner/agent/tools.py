"""Built-in agent tools: search_memory, remember, list_sources."""

from __future__ import annotations

import re
import time

from quickjoiner.config import RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.llm.base import AgentTool, ToolSpec
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore

USER_TAUGHT_SOURCE = "notes:user-taught"


def build_builtin_tools(
    store: KnowledgeStore,
    catalog: Catalog,
    pipeline: IngestPipeline,
    retrieval: RetrievalConfig,
) -> list[AgentTool]:
    def search_memory(query: str, top_k: int | None = None) -> str:
        hits = store.search(query, top_k=top_k or retrieval.top_k, min_score=retrieval.min_score)
        if not hits:
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
        return "\n\n---\n\n".join(parts)

    def remember(fact: str, topic: str | None = None) -> str:
        catalog.upsert_source(USER_TAUGHT_SOURCE, "User-taught notes", "notes")
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or fact[:40]).lower()).strip("-") or "note"
        doc = Document(
            uri=f"note://{slug}-{int(time.time())}",
            title=topic or f"Note: {fact[:60]}",
            text=fact,
            kind="note",
        )
        stats = pipeline.ingest([doc], USER_TAUGHT_SOURCE)
        return f"Remembered ({stats.summary()})."

    def list_sources() -> str:
        sources = catalog.list_sources()
        if not sources:
            return "No sources connected yet."
        lines = [
            f"- {s['id']} (type={s['type']}, documents={s['doc_count']})" for s in sources
        ]
        return "Connected sources:\n" + "\n".join(lines)

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
    ]
