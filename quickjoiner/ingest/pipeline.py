"""Ingest pipeline: Document -> normalize -> hash dedupe -> chunk -> embed ->
vector store + catalog, plus knowledge-graph maintenance: structured graph
metadata riding on Documents (deps.py dependency maps) and ticket references
extracted from any document's text are persisted as edges whose evidence is
the ingested document itself."""

from __future__ import annotations

import concurrent.futures
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from quickjoiner.config import GraphConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.connectors.deps import is_manifest_path
from quickjoiner.ingest.chunkers import chunk_document
from quickjoiner.ingest.code_graph import extract_code_graph, looks_like_code
from quickjoiner.ingest.entity_resolution import EntityResolver
from quickjoiner.ingest.normalize import normalize_text
from quickjoiner.ingest.triples import triples_to_graph
from quickjoiner.memory.catalog import Catalog
from quickjoiner.memory.store import KnowledgeStore

_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
# Uppercase-prefix-dash-number strings that are NOT ticket keys.
_TICKET_STOPLIST = {
    "UTF", "ISO", "RFC", "SHA", "MD", "AES", "RSA", "EC", "TLS", "SSL", "HTTP",
    "CVE", "GPT", "IPV", "OAUTH", "BASE", "X", "S", "EN", "A", "I18N", "L10N",
}
_MAX_TICKETS_PER_DOC = 20
# Auto-generated lockfiles: no deterministic parser bothers with these (their
# information is transitive-dependency noise, not authored relationships) and
# they're often huge, so they're excluded from LLM triple extraction the same
# way deps.py-parsed manifests are (see _triples_apply).
_LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
    "gemfile.lock", "poetry.lock", "cargo.lock", "go.sum",
}
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


def breadcrumb(source_id: str, title: str, uri: str) -> str:
    """A compact provenance/structure prefix for a chunk: "source · title · path".
    Gives every chunk's embedding the context it would otherwise be chunked away
    from (which repo/file/page it belongs to)."""
    parts: list[str] = [source_id]
    t = (title or "").strip()
    if t and t not in parts:
        parts.append(t)
    u = (uri or "").strip()
    if u and u != t and u not in parts:
        parts.append(u[:120])
    return " · ".join(p for p in parts if p)


class IngestPipeline:
    def __init__(
        self,
        store: KnowledgeStore,
        catalog: Catalog,
        retrieval: RetrievalConfig | None = None,
        graph: GraphConfig | None = None,
        triple_extractor: Callable[[str, str], list] | None = None,
        entity_resolver: EntityResolver | None = None,
        triple_workers: int = 1,
    ):
        self._store = store
        self._catalog = catalog
        # Contextual chunking is a retrieval-quality feature; when no retrieval config
        # is supplied (direct construction in tests) it stays off so chunk text is raw.
        self._contextual = bool(retrieval and retrieval.contextual_chunks)
        self._graph_cfg = graph or GraphConfig()
        # (text, title) -> list[Triple]; supplied by the app when graph.extract_triples
        # is on and an LLM is available. None => LLM triple extraction is skipped.
        self._triple_extractor = triple_extractor
        # Entity-resolution dedup (ingest/entity_resolution.py); None => every entity
        # id is created as-is (prior behavior, exact-match only).
        self._entity_resolver = entity_resolver
        # Triple extraction is one blocking LLM call per qualifying document — the
        # bottleneck on a large corpus. Documents needing it are deferred into a batch
        # and resolved with up to `triple_workers` concurrent calls after the main
        # (fast, local) chunk/embed/catalog loop finishes, instead of serializing
        # network round-trips one document at a time. 1 = fully sequential (default).
        self._triple_workers = max(1, triple_workers)

    def ingest(self, documents: Iterable[Document], source_id: str) -> IngestStats:
        stats = IngestStats()
        pending: list[tuple[str, list, list, list, str, str, str]] = []
        for doc in documents:
            try:
                self._ingest_one(doc, source_id, stats, pending)
            except Exception as exc:  # keep syncing the rest of the source
                stats.errors.append(f"{doc.uri}: {exc}")
        if pending:
            self._resolve_pending_triples(pending, source_id)
        if stats.chunks:
            ensure_index = getattr(self._store, "ensure_ann_index", None)
            if ensure_index is not None:
                ensure_index()
        return stats

    def _ingest_one(self, doc: Document, source_id: str, stats: IngestStats, pending: list) -> None:
        doc_id = _doc_id(source_id, doc.uri)
        # Normalize before hashing so cosmetic variants (curly quotes, NBSP, CRLF)
        # of the same content dedupe instead of re-embedding.
        text = normalize_text(doc.text)
        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        existing = self._catalog.get_document_hash(doc_id)
        unchanged = existing == content_hash
        if unchanged and not self._catalog.is_graph_pending(doc_id):
            stats.skipped += 1
            return
        if unchanged:
            # Content itself didn't change, but a prior run was interrupted before
            # this doc's deferred graph work (LLM triples) got persisted — retry
            # just that, skipping the redundant re-chunk/re-embed below.
            stats.skipped += 1
            self._sync_graph(doc, doc_id, source_id, text, pending)
            return

        chunks = chunk_document(text, doc.kind)
        if self._contextual and chunks:
            crumb = breadcrumb(source_id, doc.title, doc.uri)
            if crumb:
                chunks = [f"[{crumb}]\n{c}" for c in chunks]
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
        self._sync_graph(doc, doc_id, source_id, text, pending)
        stats.chunks += written
        if existing is None:
            stats.added += 1
        else:
            stats.updated += 1

    def _sync_graph(self, doc: Document, doc_id: str, source_id: str, text: str, pending: list) -> None:
        """Compute this document's knowledge-graph assertions: structured graph
        metadata (dependency maps), ticket keys, and code structure are all fast
        and deterministic, so they're persisted immediately. Optional LLM triple
        extraction is a blocking network call, so a qualifying document is queued
        into `pending` and persisted later (see `_resolve_pending_triples`) instead
        of serializing the whole ingest loop behind one LLM round-trip per doc."""
        graph = (doc.metadata or {}).get("graph") or {}
        entities = [tuple(e) for e in graph.get("entities", [])]
        alias_rows = [tuple(a) for a in graph.get("aliases", [])]
        edges = [tuple(e) for e in graph.get("edges", [])]

        src_id, src_name, src_kind = source_entity(source_id)
        src_added = False

        def _add_source_entity() -> None:
            nonlocal src_added
            if not src_added:
                entities.append((src_id, src_name, src_kind))
                src_added = True

        tickets = ticket_keys(text)
        if tickets:
            _add_source_entity()
            for key in tickets:
                entities.append((f"ticket:{key.lower()}", key, "ticket"))
                edges.append((src_id, "references", f"ticket:{key.lower()}",
                              f"mentioned in {doc.title[:80]}"))

        # Code files contribute structural edges: repo defines <symbol>, imports <module>.
        is_code = looks_like_code(doc.kind, doc.uri)
        if is_code:
            code_ents, code_edges = extract_code_graph(text, doc.uri, src_id)
            if code_edges:
                _add_source_entity()
                entities.extend(code_ents)
                edges.extend(code_edges)

        if self._triple_extractor is not None and not is_code and self._triples_apply(doc, text):
            _add_source_entity()
            # Written now (fast, synchronous) so it survives a crash/kill between
            # here and _resolve_pending_triples actually persisting this doc's
            # triples — see graph_pending's schema comment for why that matters.
            self._catalog.mark_graph_pending(doc_id, source_id)
            pending.append((doc_id, entities, alias_rows, edges, text, doc.title, doc.kind))
            return

        self._persist_graph(doc_id, entities, alias_rows, edges, source_id,
                            doc.title, doc.kind)

    def _resolve_pending_triples(self, pending: list, source_id: str) -> None:
        """Resolve the queued LLM triple-extraction calls, then persist each
        document's full graph assertions. `triple_workers > 1` runs the (network-
        bound) extractor calls concurrently; all catalog/embedding work still
        happens back on this thread as each future completes, so no locking is
        needed beyond what the catalog already provides."""
        if self._triple_workers > 1 and len(pending) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._triple_workers) as pool:
                futures = {
                    pool.submit(self._triple_extractor, text, title): (doc_id, entities, alias_rows, edges, title, kind)
                    for doc_id, entities, alias_rows, edges, text, title, kind in pending
                }
                for fut in concurrent.futures.as_completed(futures):
                    doc_id, entities, alias_rows, edges, title, kind = futures[fut]
                    try:
                        triples = fut.result()
                    except Exception:
                        triples = []
                    self._apply_triples(triples, doc_id, entities, alias_rows, edges, title, source_id, kind)
        else:
            for doc_id, entities, alias_rows, edges, text, title, kind in pending:
                try:
                    triples = self._triple_extractor(text, title)
                except Exception:
                    triples = []
                self._apply_triples(triples, doc_id, entities, alias_rows, edges, title, source_id, kind)

    def _apply_triples(self, triples: list, doc_id: str, entities: list, alias_rows: list,
                        edges: list, title: str, source_id: str, kind: str = "") -> None:
        if triples:
            g = triples_to_graph(triples, f"stated in {title[:60]}")
            entities = entities + g["entities"]
            alias_rows = alias_rows + g["aliases"]
            edges = edges + g["edges"]
        self._persist_graph(doc_id, entities, alias_rows, edges, source_id, title, kind)

    def _persist_graph(self, doc_id: str, entities: list, alias_rows: list,
                        edges: list, source_id: str,
                        doc_title: str = "", doc_kind: str = "") -> None:
        """Write entities/aliases/edges for one document. Every new entity is
        routed through the entity resolver first (if configured): a merge adds
        an alias to an existing canonical entity instead of creating a
        duplicate node, and every edge referencing the original id is remapped
        to the canonical one. Unconditional replace_doc_edges: a changed doc
        that dropped its assertions must also drop its stale edges (hash
        dedupe means we only get here on change). The evidence doc's title/kind
        ride along as adjudication context (plan 06 §1.D)."""
        context = f'mentioned in "{doc_title}" ({doc_kind})' if doc_title else ""
        id_map: dict[str, str] = {}
        for eid, name, type_ in entities:
            canonical, merged = (
                self._entity_resolver.resolve(eid, name, type_, context)
                if self._entity_resolver else (eid, False)
            )
            id_map[eid] = canonical
            if not merged:
                self._catalog.upsert_entity(canonical, name, type_, source_id)
        for alias, eid in alias_rows:
            self._catalog.add_entity_alias(alias, id_map.get(eid, eid))
        remapped = [(id_map.get(s, s), rel, id_map.get(d, d), detail) for s, rel, d, detail in edges]
        self._catalog.replace_doc_edges(doc_id, remapped)
        self._catalog.clear_graph_pending(doc_id)

    def _triples_apply(self, doc: Document, text: str) -> bool:
        """Whether a document qualifies for LLM relationship extraction: a prose-ish
        kind, long enough to be worth an LLM call, and not a manifest/lockfile —
        those are either already deterministically parsed by deps.py (redundant
        LLM call) or auto-generated transitive-dependency noise deps.py doesn't
        even bother with (package-lock.json etc.), so an LLM call on them is
        pure waste rather than a meaningful enrichment."""
        cfg = self._graph_cfg
        if doc.kind not in cfg.triple_doc_kinds or len(text) < cfg.triple_min_chars:
            return False
        path = doc.uri.split("?")[0].split("#")[0].rstrip("/").lower()
        if any(path.endswith(lf) for lf in _LOCKFILE_NAMES):
            return False
        return not is_manifest_path(doc.uri)
