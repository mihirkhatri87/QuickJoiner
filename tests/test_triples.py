"""LLM relationship extraction generalized to document ingestion: the triple→graph
conversion, the doc extractor's validation + keyless behavior, and the pipeline gate
(prose only, size threshold, code skipped, evidence = the document)."""

from __future__ import annotations

import threading

from quickjoiner.config import GraphConfig, RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.entity_resolution import EntityResolver
from quickjoiner.ingest.pipeline import IngestPipeline, _doc_id
from quickjoiner.ingest.triples import Triple, extract_doc_triples, triples_to_graph
from quickjoiner.llm.base import ChatResult

from tests.conftest import FakeEmbedder


class _Provider:
    """Minimal scripted provider: .chat(...) returns a fixed text block."""

    def __init__(self, text: str):
        self._text = text

    def chat(self, messages, system=None, **kw):
        return ChatResult(text=self._text)


# ---------------------------------------------------------------- conversion

def test_triples_to_graph_builds_entities_edges_and_aliases():
    g = triples_to_graph(
        [Triple("service", "checkout", "depends_on", "service", "payments")],
        "stated in Architecture",
    )
    names = {name for _id, name, _t in g["entities"]}
    assert names == {"checkout", "payments"}
    assert len(g["edges"]) == 1
    src, rel, dst, detail = g["edges"][0]
    assert rel == "depends_on" and detail == "stated in Architecture"
    assert src != dst


# ---------------------------------------------------------------- extractor

def test_extract_doc_triples_validates_and_drops_offvocab():
    provider = _Provider(
        "service: checkout | depends_on | service: payments\n"
        "just a sentence with no pipes\n"
        "widget: a | frobnicate | widget: b\n"          # off-vocabulary -> dropped
    )
    triples = extract_doc_triples(provider, "the doc body", "Arch")
    assert len(triples) == 1
    assert triples[0].src_name == "checkout" and triples[0].rel == "depends_on"


def test_extract_doc_triples_keyless_and_error_safe():
    assert extract_doc_triples(None, "body", "Title") == []  # no provider

    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no key")

    assert extract_doc_triples(Boom(), "body", "Title") == []  # provider failure -> []


# ---------------------------------------------------------------- pipeline gate

def _pipe(store, catalog, extractor, min_chars=10):
    return IngestPipeline(
        store, catalog, RetrievalConfig(),
        GraphConfig(extract_triples=True, triple_min_chars=min_chars),
        triple_extractor=extractor,
    )


def test_pipeline_persists_llm_triples_with_doc_evidence(store, catalog):
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return [Triple("service", "checkout", "depends_on", "service", "payments")]

    _pipe(store, catalog, extractor).ingest(
        [Document(uri="wiki/arch", title="Architecture",
                  text="The checkout service calls payments to settle orders.", kind="doc")],
        "confluence:eng",
    )
    assert calls == ["Architecture"]  # prose doc qualified
    checkout = catalog.resolve_entity("checkout")
    payments = catalog.resolve_entity("payments")
    assert checkout["type"] == "service"
    rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors(checkout["id"])}
    assert ("depends_on", payments["id"]) in rels


def test_pipeline_skips_code_and_short_docs(store, catalog):
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return []

    pipe = _pipe(store, catalog, extractor, min_chars=1000)
    pipe.ingest([Document(uri="a.py", title="Code", text="x = 1\n" * 400, kind="code")], "git:x")
    pipe.ingest([Document(uri="w", title="Short", text="too short", kind="doc")], "wiki:x")
    assert calls == []  # code skipped (structural extractor handles it); short doc under threshold


def test_pipeline_skips_manifests_and_lockfiles(store, catalog):
    """Files deps.py already parses deterministically (.csproj, package.json, ...)
    or auto-generated lockfiles are excluded from LLM triple extraction — sending
    them to the LLM would be redundant (manifests) or pure waste (lockfiles)."""
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return []

    pipe = _pipe(store, catalog, extractor, min_chars=10)
    manifest_docs = [
        Document(uri="repo::Api/Api.csproj", title="Api.csproj",
                  text="<Project>" + "x" * 400 + "</Project>", kind="doc"),
        Document(uri="repo::package.json", title="package.json",
                  text='{"name": "x", ' + "\"x\":1," * 200 + "}", kind="doc"),
        Document(uri="repo::package-lock.json", title="package-lock.json",
                  text='{"lockfileVersion": 2, ' + "\"x\":1," * 200 + "}", kind="doc"),
    ]
    pipe.ingest(manifest_docs, "git:x")
    assert calls == []

    pipe.ingest(
        [Document(uri="repo::README.md", title="README.md",
                   text="The checkout service depends on payments. " * 20, kind="doc")],
        "git:x",
    )
    assert calls == ["README.md"]  # ordinary prose still qualifies


def test_pipeline_without_extractor_does_nothing(store, catalog):
    # No extractor supplied -> no LLM path, no error (the common/default case).
    IngestPipeline(store, catalog, RetrievalConfig()).ingest(
        [Document(uri="w", title="Doc", text="The checkout service calls payments.", kind="doc")],
        "wiki:x",
    )
    assert catalog.resolve_entity("checkout") is None


# --------------------------------------------------------- concurrent extraction

def test_pipeline_resolves_triples_concurrently_across_a_batch(store, catalog):
    """triple_workers > 1: every qualifying doc in the batch still gets its
    triples persisted correctly (order-independent), and the extractor is
    actually invoked from multiple threads, not serialized."""
    seen_threads: set[int] = set()
    lock = threading.Lock()

    def extractor(text, title):
        with lock:
            seen_threads.add(threading.get_ident())
        n = title[-1]
        return [Triple("service", f"svc{n}", "depends_on", "service", f"dep{n}")]

    docs = [
        Document(uri=f"wiki/{i}", title=f"Page {i}",
                  text=f"Service svc{i} depends on dep{i} for processing.", kind="doc")
        for i in range(6)
    ]
    pipe = IngestPipeline(store, catalog, RetrievalConfig(), GraphConfig(extract_triples=True, triple_min_chars=1),
                           triple_extractor=extractor, triple_workers=4)
    stats = pipe.ingest(docs, "confluence:eng")
    assert stats.added == 6 and not stats.errors

    for i in range(6):
        svc = catalog.resolve_entity(f"svc{i}")
        dep = catalog.resolve_entity(f"dep{i}")
        assert svc and dep
        rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors(svc["id"])}
        assert ("depends_on", dep["id"]) in rels
    assert len(seen_threads) > 1  # actually ran across multiple worker threads


def test_pipeline_concurrent_triples_survive_extractor_failures(store, catalog):
    """One doc's extractor call raising must not lose the others in the batch
    (mirrors the existing sequential fire-and-forget guarantee)."""
    def extractor(text, title):
        if "Bad" in title:
            raise RuntimeError("boom")
        return [Triple("service", "ok-service", "depends_on", "service", "ok-dep")]

    docs = [
        Document(uri="wiki/good", title="Good Page", text="A" * 50, kind="doc"),
        Document(uri="wiki/bad", title="Bad Page", text="B" * 50, kind="doc"),
    ]
    pipe = IngestPipeline(store, catalog, RetrievalConfig(), GraphConfig(extract_triples=True, triple_min_chars=1),
                           triple_extractor=extractor, triple_workers=4)
    stats = pipe.ingest(docs, "confluence:eng")
    assert stats.added == 2 and not stats.errors  # a triple-extraction failure never fails ingest
    assert catalog.resolve_entity("ok-service") is not None


# --------------------------------------------------- crash/interruption recovery

def test_interrupted_batch_is_retried_on_next_sync(store, catalog):
    """Simulates a kill between the fast path (content_hash already written) and
    the deferred triple batch persisting: mark_graph_pending survives that, so a
    later sync with unchanged content still retries the graph work instead of
    silently skipping the document forever (the gap a real 3+ hour backfill
    getting killed mid-batch actually hit)."""
    doc = Document(uri="wiki/x", title="X Page",
                    text="The checkout service depends on payments for settlement.", kind="doc")
    doc_id = _doc_id("confluence:eng", doc.uri)

    # Pass 1: no extractor configured -> content indexed, nothing graph-related queued.
    IngestPipeline(store, catalog, RetrievalConfig()).ingest([doc], "confluence:eng")
    assert catalog.resolve_entity("checkout") is None

    # Simulate the crash: this doc's graph work was queued but the process died
    # before _resolve_pending_triples ever persisted it.
    catalog.mark_graph_pending(doc_id, "confluence:eng")

    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return [Triple("service", "checkout", "depends_on", "service", "payments")]

    # Pass 2: identical content (same hash) but graph_pending=True -> must NOT be
    # skipped as "unchanged"; the triple extraction must actually run this time.
    stats = IngestPipeline(
        store, catalog, RetrievalConfig(), GraphConfig(extract_triples=True, triple_min_chars=1),
        triple_extractor=extractor,
    ).ingest([doc], "confluence:eng")

    assert calls == ["X Page"]
    assert stats.chunks == 0  # content unchanged -> no wasted re-chunk/re-embed
    checkout = catalog.resolve_entity("checkout")
    assert checkout is not None
    rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors(checkout["id"])}
    assert ("depends_on", catalog.resolve_entity("payments")["id"]) in rels
    assert not catalog.is_graph_pending(doc_id)


def test_unchanged_doc_without_pending_flag_is_still_skipped(store, catalog):
    """The common case is untouched: an unchanged doc with no stranded graph
    work is skipped outright, no extractor call spent re-checking it."""
    doc = Document(uri="wiki/y", title="Y", text="Some ordinary unrelated prose here.", kind="doc")
    calls: list[str] = []

    def extractor(text, title):
        calls.append(title)
        return []

    pipe = IngestPipeline(store, catalog, RetrievalConfig(), GraphConfig(extract_triples=True, triple_min_chars=1),
                           triple_extractor=extractor)
    first = pipe.ingest([doc], "confluence:eng")
    second = pipe.ingest([doc], "confluence:eng")
    assert first.added == 1 and second.skipped == 1
    assert calls == ["Y"]  # only the first pass triggered extraction


# ----------------------------------------------------- entity resolver wiring

def test_pipeline_routes_triple_entities_through_resolver(store, catalog):
    """When an entity_resolver is configured, a triple-derived entity that the
    resolver merges into an existing one produces an alias + remapped edges,
    not a duplicate node."""
    catalog.upsert_entity("project:appriver-connector-web", "AppRiver Connector Web", "project")
    resolver = EntityResolver(
        catalog=catalog, embedder=FakeEmbedder(),
        adjudicate=lambda type_, name, candidates: candidates[0],
    )

    def extractor(text, title):
        return [Triple("project", "Connector Web Service", "part_of", "service", "secure-cloud")]

    pipe = IngestPipeline(store, catalog, RetrievalConfig(), GraphConfig(extract_triples=True, triple_min_chars=1),
                           triple_extractor=extractor, entity_resolver=resolver)
    pipe.ingest(
        [Document(uri="wiki/adr", title="ADR", text="Connector Web Service is part of Secure Cloud.", kind="doc")],
        "confluence:eng",
    )
    # Merged into the pre-existing canonical entity, not a new "connector web service" node.
    assert catalog.resolve_entity("project:connector-web-service") is None
    canonical = catalog.resolve_entity("connector web service")
    assert canonical and canonical["id"] == "project:appriver-connector-web"
    rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors("project:appriver-connector-web")}
    assert ("part_of", "service:secure-cloud") in rels
