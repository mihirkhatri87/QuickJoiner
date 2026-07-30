"""Ingest pipeline: Document -> normalize -> hash dedupe -> chunk -> embed ->
vector store + catalog, plus knowledge-graph maintenance: structured graph
metadata riding on Documents (deps.py dependency maps) and ticket references
extracted from any document's text are persisted as edges whose evidence is
the ingested document itself."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from quickjoiner.config import GraphConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.connectors.deps import is_manifest_path
from quickjoiner.ingest.chunkers import chunk_document
from quickjoiner.ingest.code_graph import extract_code_graph, looks_like_code
from quickjoiner.ingest.pubsub import extract_pubsub_graph
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
class DrainStats:
    """Outcome of a graph-pending drain (see `IngestPipeline.drain_pending_graph`)."""
    documents: int = 0     # documents whose graph was rebuilt and pending cleared
    faithful: int = 0      # ...of which carried their stored deterministic payload
    text_only: int = 0     # ...of which predate it, so only stored text could be re-mined
    missing_text: int = 0  # no chunks left to read: left pending rather than guessed at
    orphans: int = 0       # pending rows for documents that no longer exist
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.documents} documents drained"]
        if self.faithful:
            parts.append(f"{self.faithful} rebuilt in full")
        if self.text_only:
            parts.append(f"{self.text_only} re-mined from stored text only")
        if self.missing_text:
            parts.append(f"{self.missing_text} skipped (no stored text)")
        if self.orphans:
            parts.append(f"{self.orphans} orphan rows swept")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


@dataclass
class IngestStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    chunks: int = 0
    graph_refreshed: int = 0  # unchanged docs whose graph was rebuilt (extractors bumped)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.added} added, {self.updated} updated, {self.skipped} unchanged, "
            f"{self.chunks} chunks written"
            + (f", {self.graph_refreshed} graph-refreshed" if self.graph_refreshed else "")
            + (f", {len(self.errors)} errors" if self.errors else "")
        )


# Bump when the DETERMINISTIC graph extractors change (a new/changed edge shape from
# code_graph / pubsub / deps / ticket keys / a connector's `metadata["graph"]` such as ADO
# dev-links or GitLab MRs). A plain sync that re-fetches a doc whose content is unchanged but
# whose stored `graph_version` is below this rebuilds its graph edges WITHOUT re-embedding —
# so a graph-only feature rolls out on the next ordinary sync instead of needing a clean
# re-sync. History: v2 (2026-07-23) = ADO Development-link edges + GitLab MR/branch graph.
# v3 (2026-07-26) = ADO Hierarchy (part_of)/Related (related_to) edges + per-doc display
# metadata (work item type/state/team/sprint/parent id) for the document browser's tree view.
GRAPH_EXTRACTOR_VERSION = 3


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

    @property
    def extracts_triples(self) -> bool:
        """Whether LLM relationship extraction is wired up (`graph.extract_triples` +
        a reachable provider). Callers that only make sense with it — the pending drain —
        check this rather than reaching for the private attribute."""
        return self._triple_extractor is not None

    def ingest(self, documents: Iterable[Document], source_id: str,
               control: Any = None,
               progress_cb: Callable[[int, int], None] | None = None) -> IngestStats:
        """`control` (optional) lets a caller pause/stop the deferred graph-extraction
        phase: any object with `proceed() -> bool` that blocks while paused and returns
        False once cancelled (see `sync_manager._PauseControl`). None = run to completion,
        the behaviour for the CLI, scheduler and tests. The document loop itself is gated
        by whatever iterator is passed in (the sync manager wraps it).

        `progress_cb` (optional) reports (chunks embedded, total chunks) for the chunk/embed
        step of a document that actually needs it (unchanged docs skip straight past it).
        It exists for exactly one caller — the ad-hoc "learn this attachment now" API route,
        where a single large document can take minutes to embed on a CPU-only machine and the
        UI needs something honest to show — not for connector syncs, which report progress
        through `SyncControl.stage()` instead. Only meaningful with ONE document; with several,
        each document's progress simply overwrites the last (fine for its one real caller)."""
        stats = IngestStats()
        pending: list[tuple[str, list, list, list, str, str, str]] = []
        for doc in documents:
            try:
                self._ingest_one(doc, source_id, stats, pending, progress_cb)
            except Exception as exc:  # keep syncing the rest of the source
                stats.errors.append(f"{doc.uri}: {exc}")
        if pending:
            self._resolve_pending_triples(pending, source_id, control)
        if stats.added or stats.updated:
            # Recompute the derived cross-source identity bridges (ingest/bridges.py) now
            # that this batch's entities are in — cheap (O(entities)), idempotent, and
            # best-effort: the bridge layer is an enrichment, never worth failing a sync.
            refresh = getattr(self._catalog, "refresh_same_as_bridges", None)
            if refresh is not None:
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    pass
        if stats.chunks:
            ensure_index = getattr(self._store, "ensure_ann_index", None)
            if ensure_index is not None:
                ensure_index()
            # Reclaim the LanceDB versions this batch's deletes/adds superseded. Throttled
            # inside the store (only fires once enough versions accumulate), so a stream of
            # small incremental syncs doesn't pay for a full optimize each time. Best-effort
            # and absent on the Postgres store (autovacuum handles it there) — hence getattr.
            maybe_compact = getattr(self._store, "maybe_compact", None)
            if maybe_compact is not None:
                maybe_compact()
        return stats

    def _ingest_one(self, doc: Document, source_id: str, stats: IngestStats, pending: list,
                     progress_cb: Callable[[int, int], None] | None = None) -> None:
        doc_id = _doc_id(source_id, doc.uri)
        # Normalize before hashing so cosmetic variants (curly quotes, NBSP, CRLF)
        # of the same content dedupe instead of re-embedding.
        text = normalize_text(doc.text)
        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        existing = self._catalog.get_document_hash(doc_id)
        unchanged = existing == content_hash
        if unchanged:
            graph_pending = self._catalog.is_graph_pending(doc_id)
            stale_graph = self._catalog.get_document_graph_version(doc_id) < GRAPH_EXTRACTOR_VERSION
            if not graph_pending and not stale_graph:
                stats.skipped += 1
                return
            # Content didn't change, but either a prior run was interrupted before this doc's
            # deferred graph work persisted (graph_pending) OR the deterministic extractors
            # changed since it was last built (stale_graph). Rebuild the graph from the
            # freshly-provided doc (its metadata — e.g. ADO relations — is available) WITHOUT
            # re-chunk/re-embed. NB: if LLM triple extraction is enabled this re-queues it;
            # with it off (the default) the refresh is purely deterministic and cheap.
            stats.skipped += 1
            self._sync_graph(doc, doc_id, source_id, text, pending)
            # Display metadata (e.g. ADO team/sprint/state) backfills the same way the graph
            # does: an unchanged document still carries whatever the connector freshly
            # computed this pull, and this is a standalone UPDATE — no re-embed. Without it,
            # team/sprint/state would stay blank on every already-ingested work item until
            # its content next actually changes, the same staleness bug the graph refresh
            # above exists to avoid, just for metadata instead of edges.
            display = (doc.metadata or {}).get("display")
            if display is not None:
                self._catalog.update_document_metadata(doc_id, json.dumps(display))
            if stale_graph and not graph_pending:
                # graph_pending docs get their version stamped when the pending drains;
                # a pure version-refresh has no pending work, so stamp it now.
                self._catalog.set_document_graph_version(doc_id, GRAPH_EXTRACTOR_VERSION)
                stats.graph_refreshed += 1
            return

        chunks = chunk_document(text, doc.kind)
        if self._contextual and chunks:
            crumb = breadcrumb(source_id, doc.title, doc.uri)
            if crumb:
                chunks = [f"[{crumb}]\n{c}" for c in chunks]
        # Batched, progress-reporting embed only when a caller actually wants it AND the
        # store supports it (on-prem LanceDB only today — Postgres/pgvector falls back to
        # the plain call below, same as ensure_ann_index/maybe_compact elsewhere in this
        # pipeline). Every connector sync passes progress_cb=None, so this is the exact
        # same call as before for the hot path.
        upsert_with_progress = getattr(self._store, "upsert_document_with_progress", None)
        if progress_cb is not None and upsert_with_progress is not None:
            written = upsert_with_progress(
                doc_id=doc_id,
                source_id=source_id,
                uri=doc.uri,
                title=doc.title,
                kind=doc.kind,
                chunks=chunks,
                updated_at=doc.updated_at or "",
                on_progress=progress_cb,
            )
        else:
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
            graph_version=GRAPH_EXTRACTOR_VERSION,  # freshly built with the current extractors
            metadata_json=json.dumps((doc.metadata or {}).get("display") or {}),
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

        # Runtime coupling (pub/sub channels, datastores) from code, app config and
        # infra templates — the edges package manifests can't see (ingest/pubsub.py).
        ps_ents, ps_edges = extract_pubsub_graph(text, doc.uri, src_id)
        if ps_edges:
            _add_source_entity()
            entities.extend(ps_ents)
            edges.extend(ps_edges)

        if self._triple_extractor is not None and not is_code and self._triples_apply(doc, text):
            _add_source_entity()
            # Written now (fast, synchronous) so it survives a crash/kill between
            # here and _resolve_pending_triples actually persisting this doc's
            # triples — see graph_pending's schema comment for why that matters.
            # The deterministic assertions ride along so a later drain can finish this
            # document even if its connector never re-yields it (a moving-window
            # connector's aged-out work item), rather than re-deriving only the part
            # that is still visible in the stored text.
            self._catalog.mark_graph_pending(
                doc_id, source_id,
                json.dumps({"entities": entities, "aliases": alias_rows, "edges": edges}),
            )
            pending.append((doc_id, entities, alias_rows, edges, text, doc.title, doc.kind))
            return

        self._persist_graph(doc_id, entities, alias_rows, edges, source_id,
                            doc.title, doc.kind)

    # ---------------------------------------------------------------- drain
    def drain_pending_graph(self, source_id: str | None = None, control: Any = None,
                            log: Callable[[str], None] | None = None) -> DrainStats:
        """Finish the deferred graph work for documents whose connector will never
        re-provide them (AI_ROADMAP #25).

        `graph_pending` rows are normally retried by the next sync that re-yields the
        document — which never comes for a connector that ingests a moving window (a TFS
        work item that has aged out of every team's recent-sprint slice). Those documents
        keep their chunks, their vectors and their citations, and have **no edges at all**,
        because `_sync_graph` defers a qualifying document's whole graph — deterministic
        assertions included — until its LLM triples resolve. This pass re-reads the text
        that was actually indexed and finishes them in place, with no connector round-trip.

        Faithfulness is explicit, not assumed: rows written since `graph_pending.graph_json`
        exists carry the exact deterministic payload computed at ingest (a connector's own
        dev-link/hierarchy edges included), so they rebuild completely. Rows that predate
        it can only be rebuilt from stored text — ticket keys, code structure, pub/sub —
        and are counted separately so the log can say which is which instead of implying a
        full recovery. A document whose chunks are gone is left pending, never guessed at.
        """
        stats = DrainStats()
        stats.orphans = self._catalog.sweep_orphan_graph_pending()
        rows = self._catalog.list_graph_pending(source_id)
        if not rows:
            return stats
        by_source: dict[str, list[dict]] = {}
        for row in rows:
            by_source.setdefault(row["source_id"], []).append(row)
        for src, group in by_source.items():
            if log:
                log(f"{src}: {len(group)} documents with unmined relationships")
            texts = self._stored_texts(group)
            pending: list = []
            for row in group:
                if control is not None:
                    control.check()  # this phase is fast+local, but a stop shouldn't wait for it
                text = texts.get(row["doc_id"], "")
                if not text.strip():
                    stats.missing_text += 1
                    continue
                raw = row.get("graph_json") or ""
                metadata: dict = {}
                if raw:
                    try:
                        metadata["graph"] = json.loads(raw)
                        stats.faithful += 1
                    except ValueError:
                        stats.text_only += 1
                else:
                    stats.text_only += 1
                doc = Document(uri=row["uri"] or "", title=row["title"] or "",
                               text=text, kind=row["kind"] or "doc", metadata=metadata)
                try:
                    self._sync_graph(doc, row["doc_id"], src, text, pending)
                    stats.documents += 1
                except Exception as exc:  # one bad document never stops the drain
                    stats.errors.append(f"{row['uri']}: {exc}")
            if pending:
                self._resolve_pending_triples(pending, src, control)
        return stats

    def _stored_texts(self, rows: list[dict]) -> dict[str, str]:
        """Reconstruct each document's text from the chunks that were indexed for it.

        Contextual chunking prepends a `[source · title · path]` breadcrumb to every
        chunk before embedding; it is stripped back off here so the rebuilt text is the
        document, not the document with its provenance line repeated N times."""
        doc_ids = [r["doc_id"] for r in rows]
        batch = getattr(self._store, "get_documents_chunks", None)
        chunks_by_doc = (
            batch(doc_ids) if batch is not None
            else {d: self._store.get_document_chunks(d) for d in doc_ids}
        )
        out: dict[str, str] = {}
        for row in rows:
            chunks = chunks_by_doc.get(row["doc_id"]) or []
            crumb = f"[{breadcrumb(row['source_id'], row['title'] or '', row['uri'] or '')}]\n"
            out[row["doc_id"]] = "\n\n".join(
                c[len(crumb):] if c.startswith(crumb) else c for c in chunks
            )
        return out

    def _resolve_pending_triples(self, pending: list, source_id: str,
                                 control: Any = None) -> None:
        """Resolve the queued LLM triple-extraction calls, then persist each document's
        full graph assertions. Drained in small waves (one per worker pool) so pause/stop
        via `control` takes effect promptly — on a big corpus this is the long tail
        (thousands of broker calls), so it must honor stop within seconds, not run to
        completion. `control.stage()` before each wave both reports "graph relationships
        (done/total)" for the UI and raises SyncStopped on cancel; a cancel leaves the
        undrained docs in `graph_pending`, so they resolve on a later sync/drain — safe and
        idempotent, never lost work. Wave = `triple_workers`, so at most that many calls
        are ever in flight when a stop lands."""
        total = len(pending)
        wave = max(1, self._triple_workers)
        done = 0
        for i in range(0, total, wave):
            if control is not None:
                control.stage("graph relationships", done, total)  # checkpoint + progress
            chunk = pending[i:i + wave]
            self._drain_pending(chunk, source_id)
            done += len(chunk)
        if control is not None:
            control.stage("graph relationships", done, total)

    def _drain_pending(self, pending: list, source_id: str) -> None:
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
        # The graph for this doc is now fully built with the current extractors — stamp the
        # version so a stale-graph refresh (or graph_pending retry) doesn't fire again next sync.
        self._catalog.set_document_graph_version(doc_id, GRAPH_EXTRACTOR_VERSION)

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
