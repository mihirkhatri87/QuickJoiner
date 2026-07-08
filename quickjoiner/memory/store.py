"""LanceDB-backed vector store for learned knowledge chunks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import lancedb

from quickjoiner.memory.embedder import Embedder

_TABLE = "chunks"


@dataclass
class SearchHit:
    text: str
    score: float
    doc_id: str
    source_id: str
    uri: str
    title: str
    kind: str


class KnowledgeStore:
    def __init__(self, workspace: Path, embedder: Embedder):
        self._db = lancedb.connect(str(workspace / "lancedb"))
        self._embedder = embedder

    def _table(self):
        if _TABLE in self._db.list_tables().tables:
            return self._db.open_table(_TABLE)
        return None

    def upsert_document(
        self,
        doc_id: str,
        source_id: str,
        uri: str,
        title: str,
        kind: str,
        chunks: list[str],
        updated_at: str = "",
    ) -> int:
        table = self._table()
        if table is not None:
            table.delete(f'doc_id = "{doc_id}"')
        if not chunks:
            return 0
        vectors = self._embedder.embed(chunks)
        rows = [
            {
                "id": f"{doc_id}#{i}",
                "doc_id": doc_id,
                "source_id": source_id,
                "uri": uri,
                "title": title or "",
                "kind": kind,
                "updated_at": updated_at,
                "chunk_index": i,
                "text": chunk,
                "vector": vec,
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        if table is None:
            self._db.create_table(_TABLE, data=rows)
        else:
            table.add(rows)
        return len(rows)

    def delete_document(self, doc_id: str) -> None:
        table = self._table()
        if table is not None:
            table.delete(f'doc_id = "{doc_id}"')

    def delete_source(self, source_id: str) -> None:
        table = self._table()
        if table is not None:
            table.delete(f'source_id = "{source_id}"')

    def search(self, query: str, top_k: int = 8, min_score: float = 0.0) -> list[SearchHit]:
        table = self._table()
        if table is None:
            return []
        vector = self._embedder.embed_query(query)
        results = (
            table.search(vector, vector_column_name="vector")
            .metric("cosine")
            .limit(top_k)
            .to_list()
        )
        hits = []
        for r in results:
            score = 1.0 - float(r.get("_distance", 1.0))
            if score < min_score:
                continue
            hits.append(
                SearchHit(
                    text=r["text"],
                    score=score,
                    doc_id=r["doc_id"],
                    source_id=r["source_id"],
                    uri=r["uri"],
                    title=r.get("title", ""),
                    kind=r.get("kind", "doc"),
                )
            )
        return hits
