"""Ingest pipeline: Document -> normalize -> hash dedupe -> chunk -> embed ->
vector store + catalog, plus knowledge-graph maintenance: structured graph
metadata riding on Documents (deps.py dependency maps) and ticket references
extracted from any document's text are persisted as edges whose evidence is
the ingested document itself."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from quickjoiner.connectors.base import Document
from quickjoiner.ingest.chunkers import chunk_document
from quickjoiner.ingest.normalize import normalize_text
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore

_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
# Uppercase-prefix-dash-number strings that are NOT ticket keys.
_TICKET_STOPLIST = {
    "UTF", "ISO", "RFC", "SHA", "MD", "AES", "RSA", "EC", "TLS", "SSL", "HTTP",
    "CVE", "GPT", "IPV", "OAUTH", "BASE", "X", "S", "EN", "A", "I18N", "L10N",
}
_MAX_TICKETS_PER_DOC = 20
# "files" included: a folder-ingested project is the same entity the dependency
# map calls "repo:<name>", so ticket references land on the same node.
_REPO_SOURCE_TYPES = {"git", "github", "gitlab", "azure_devops", "files"}


def ticket_keys(text: str) -> list[str]:
    """Distinct Jira/ADO-style ticket keys (NAUT-123) in reading order."""
    out: list[str] = []
    for key in _TICKET.findall(text):
        if key.split("-", 1)[0] in _TICKET_STOPLIST:
            continue
        if key not in out:
            out.append(key)
            if len(out) >= _MAX_TICKETS_PER_DOC:
                break
    return out


def source_entity(source_id: str) -> tuple[str, str, str]:
    """(entity_id, name, type) for the source a document came from — repos keep
    their identity ("repo:proj-a"); other sources are generic containers."""
    type_, _, name = source_id.partition(":")
    if not name:
        type_, name = "source", source_id
    kind = "repo" if type_ in _REPO_SOURCE_TYPES else "source"
    return (f"{kind}:{name.lower()}", name, kind)


@dataclass
class IngestStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.added} added, {self.updated} updated, {self.skipped} unchanged, "
            f"{self.chunks} chunks written"
            + (f", {len(self.errors)} errors" if self.errors else "")
        )


def _doc_id(source_id: str, uri: str) -> str:
    return hashlib.sha256(f"{source_id}|{uri}".encode()).hexdigest()[:24]


class IngestPipeline:
    def __init__(self, store: KnowledgeStore, catalog: Catalog):
        self._store = store
        self._catalog = catalog

    def ingest(self, documents: Iterable[Document], source_id: str) -> IngestStats:
        stats = IngestStats()
        for doc in documents:
            try:
                self._ingest_one(doc, source_id, stats)
            except Exception as exc:  # keep syncing the rest of the source
                stats.errors.append(f"{doc.uri}: {exc}")
        if stats.chunks:
            ensure_index = getattr(self._store, "ensure_ann_index", None)
            if ensure_index is not None:
                ensure_index()
        return stats

    def _ingest_one(self, doc: Document, source_id: str, stats: IngestStats) -> None:
        doc_id = _doc_id(source_id, doc.uri)
        # Normalize before hashing so cosmetic variants (curly quotes, NBSP, CRLF)
        # of the same content dedupe instead of re-embedding.
        text = normalize_text(doc.text)
        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        existing = self._catalog.get_document_hash(doc_id)
        if existing == content_hash:
            stats.skipped += 1
            return

        chunks = chunk_document(text, doc.kind)
        written = self._store.upsert_document(
            doc_id=doc_id,
            source_id=source_id,
            uri=doc.uri,
            title=doc.title,
            kind=doc.kind,
            chunks=chunks,
            updated_at=doc.updated_at or "",
        )
        self._catalog.upsert_document(
            doc_id=doc_id,
            source_id=source_id,
            uri=doc.uri,
            title=doc.title,
            kind=doc.kind,
            content_hash=content_hash,
            updated_at=doc.updated_at,
            chunk_count=written,
        )
        self._sync_graph(doc, doc_id, source_id, text)
        stats.chunks += written
        if existing is None:
            stats.added += 1
        else:
            stats.updated += 1

    def _sync_graph(self, doc: Document, doc_id: str, source_id: str, text: str) -> None:
        """Persist this document's knowledge-graph assertions: structured graph
        metadata (dependency maps) plus ticket keys found in the text. Edges are
        replaced per evidence document, so re-ingest refreshes and never dupes."""
        graph = (doc.metadata or {}).get("graph") or {}
        entities = [tuple(e) for e in graph.get("entities", [])]
        alias_rows = [tuple(a) for a in graph.get("aliases", [])]
        edges = [tuple(e) for e in graph.get("edges", [])]

        tickets = ticket_keys(text)
        if tickets:
            src_id, src_name, src_kind = source_entity(source_id)
            entities.append((src_id, src_name, src_kind))
            for key in tickets:
                entities.append((f"ticket:{key.lower()}", key, "ticket"))
                edges.append((src_id, "references", f"ticket:{key.lower()}",
                              f"mentioned in {doc.title[:80]}"))

        for eid, name, type_ in entities:
            self._catalog.upsert_entity(eid, name, type_, source_id)
        for alias, eid in alias_rows:
            self._catalog.add_entity_alias(alias, eid)
        # Unconditional: a changed doc that dropped its assertions must also
        # drop its stale edges (hash dedupe means we only get here on change).
        self._catalog.replace_doc_edges(doc_id, edges)
