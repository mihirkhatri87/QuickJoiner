"""Ingest pipeline: Document -> hash dedupe -> chunk -> embed -> vector store + catalog."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

from quickjoiner.connectors.base import Document
from quickjoiner.ingest.chunkers import chunk_document
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore


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
        return stats

    def _ingest_one(self, doc: Document, source_id: str, stats: IngestStats) -> None:
        doc_id = _doc_id(source_id, doc.uri)
        content_hash = hashlib.sha256(doc.text.encode("utf-8", errors="replace")).hexdigest()
        existing = self._catalog.get_document_hash(doc_id)
        if existing == content_hash:
            stats.skipped += 1
            return

        chunks = chunk_document(doc.text, doc.kind)
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
        stats.chunks += written
        if existing is None:
            stats.added += 1
        else:
            stats.updated += 1
